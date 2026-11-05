"""MIDI bridge between the MCP server and the Cubase MIDI Remote script.

Backends:
  - "rtmidi": real MIDI via python-rtmidi through a virtual port
    (macOS: IAC Driver Bus 1 / Windows: loopMIDI)
  - "dummy": in-process fake Cubase responder for dry testing without Cubase
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional

import mido

from . import protocol as P

log = logging.getLogger("cubase_mcp.bridge")

NUM_SLOTS = P.CC.NUM_CHANNELS
NUM_QC = P.CC.NUM_QC

TRANSPORT_ACTIONS = {
    "play": P.CC.play,
    "stop": P.CC.stop,
    "record": P.CC.record,
    "cycle": P.CC.cycle,
    "metronome": P.CC.metronome,
    "rewind": P.CC.rewind,
    "forward": P.CC.forward,
}

BANK_ACTIONS = {"next": 0, "prev": 1, "shift_left": 2, "shift_right": 3, "reset": 4}

TOGGLE_SYSEX = {
    "mute": P.SX.SET_MUTE,
    "solo": P.SX.SET_SOLO,
    "record_arm": P.SX.SET_RECARM,
    "monitor": P.SX.SET_MONITOR,
}


class BridgeError(RuntimeError):
    pass


def resolve_port(name: Optional[str], candidates: List[str], direction: str) -> str:
    """Exact match first, then case-insensitive substring match."""
    if name and name in candidates:
        return name
    if name:
        lowered = name.lower()
        for c in candidates:
            if lowered in c.lower():
                return c
        raise BridgeError(
            f"{direction} MIDI port matching '{name}' not found. "
            f"Available: {candidates}"
        )
    # auto-detect
    patterns = ["iac driver bus", "loopmidi", "mcp bridge"]
    for pattern in patterns:
        for c in candidates:
            if pattern in c.lower():
                return c
    raise BridgeError(
        f"No virtual MIDI {direction} port found. "
        f"Enable the IAC driver (macOS) or loopMIDI (Windows). Available: {candidates}"
    )


class MidiBridge:
    def __init__(
        self,
        in_port: Optional[str] = None,
        out_port: Optional[str] = None,
        backend: str = "rtmidi",
        virtual: bool = False,
    ) -> None:
        self.backend = backend
        self.virtual = virtual
        self._in_port_name = in_port
        self._out_port_name = out_port
        self._mido_in: Optional[mido.ports.BaseInput] = None
        self._mido_out: Optional[mido.ports.BaseOutput] = None
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._recent: deque = deque(maxlen=200)
        self._ident_event = threading.Event()
        self._da_param_changes: deque = deque(maxlen=100)
        self._da_pc_counter = 0  # monotonic index; deque alone cannot address past events
        self._selected_name: Optional[str] = None
        self._da_walks: Dict[int, dict] = {}  # walk_id -> per-walk buffers (per-request isolation)
        self._da_walk_gen = 0  # increments per da_walk() request
        self._fake: Optional["_FakeCubase"] = None
        self._started = False
        self._degraded_reason: Optional[str] = None

        ident_default = {"ident": None}
        transport_default = {
            "play": False,
            "record": False,
            "cycle": False,
            "metronome": False,
            "tempo_bpm": 0.0,
            "locator": "",
            "locator_format": "",
        }
        plugin_default = {"role": 0, "name": "", "vendor": "", "version": ""}
        self._state: Dict = {
            **ident_default,
            "transport": transport_default,
            "plugin": plugin_default,
            "slots": [
                {
                    "slot": i,
                    "exists": False,
                    "name": "",
                    "selected": False,
                    "mute": False,
                    "solo": False,
                    "record_arm": False,
                    "monitor": False,
                    "volume": 0.0,
                    "pan": 0.5,
                }
                for i in range(NUM_SLOTS)
            ],
            "qc": [0.0] * NUM_QC,
            "cc_feedback": {},
        }

    # ------------------------------------------------------------------ life
    def start(self) -> None:
        if self._started:
            return
        if self.backend == "dummy":
            self._start_dummy()
        else:
            self._start_rtmidi()
        self._started = True
        # ask the script for a full state dump (also identifies the script)
        try:
            self._send_sysex_data(P.cmd_ping())
        except Exception as exc:  # port may not be ready yet
            log.warning("initial ping failed: %s", exc)

    def stop(self) -> None:
        if self._mido_in:
            self._mido_in.close()
            self._mido_in = None
        if self._mido_out:
            self._mido_out.close()
            self._mido_out = None
        self._started = False

    def _start_rtmidi(self) -> None:
        if self.virtual:
            # CoreMIDI virtual ports: no IAC/loopMIDI setup required. The
            # Cubase script's detection units already match the "MCP Bridge"
            # port names.
            in_name = self._in_port_name or "MCP Bridge In"
            out_name = self._out_port_name or "MCP Bridge Out"
            log.info("opening VIRTUAL MIDI ports '%s' / '%s'", in_name, out_name)
            self._mido_in = mido.open_input(in_name, virtual=True, callback=self._on_mido_message)
            self._mido_out = mido.open_output(out_name, virtual=True)
            return
        inputs = mido.get_input_names()
        outputs = mido.get_output_names()
        in_name = resolve_port(self._in_port_name, inputs, "input")
        out_name = resolve_port(self._out_port_name, outputs, "output")
        log.info("opening MIDI input '%s' / output '%s'", in_name, out_name)
        self._mido_in = mido.open_input(in_name, callback=self._on_mido_message)
        self._mido_out = mido.open_output(out_name)

    def _start_dummy(self) -> None:
        self._fake = _FakeCubase(self._on_mido_message)
        log.info("dummy backend: no real MIDI ports are used")

    def mark_degraded(self, reason: str) -> None:
        """Record why the MIDI ports are unavailable (degraded start)."""
        self._degraded_reason = reason

    def _require_ready(self) -> None:
        if self._degraded_reason:
            raise BridgeError(
                f"{self._degraded_reason} "
                "(enable the virtual MIDI port, then restart the server)"
            )

    # ----------------------------------------------------------------- input
    def _on_mido_message(self, msg: mido.Message) -> None:
        try:
            if msg.type == "sysex":
                data = list(msg.data)
                event = P.parse_feedback(data)
                if event is None:
                    return
                with self._cond:
                    self._recent.append({"dir": "in", "sysex": event})
                    self._apply_event(event)
                    self._cond.notify_all()
            elif msg.type == "control_change":
                with self._cond:
                    self._recent.append(
                        {"dir": "in", "cc": msg.control, "value": msg.value}
                    )
                    self._state["cc_feedback"][str(msg.control)] = msg.value
                    self._cond.notify_all()
        except Exception:
            log.exception("error handling MIDI message")

    def _apply_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "ident":
            self._state["ident"] = event.get("text")
            self._ident_event.set()
        elif etype == "transport":
            self._state["transport"].update(
                {k: v for k, v in event.items() if k != "type"}
            )
        elif etype == "slot":
            slot = event.get("slot", -1)
            if 0 <= slot < NUM_SLOTS:
                self._state["slots"][slot].update(
                    {k: v for k, v in event.items() if k not in ("type", "slot")}
                )
                self._state["slots"][slot]["slot"] = slot
        elif etype == "qc":
            idx = event.get("index", -1)
            if 0 <= idx < NUM_QC:
                self._state["qc"][idx] = event.get("value", 0.0)
        elif etype == "plugin":
            self._state["plugin"] = {
                k: v for k, v in event.items() if k != "type"
            }
        elif etype == "selected_name":
            self._selected_name = event.get("name", "")
            for s in self._state["slots"]:
                if s["selected"]:
                    if s["name"] != self._selected_name:
                        s["name"] = self._selected_name
                        s["exists"] = True
        elif etype == "da_param_change":
            self._da_pc_counter += 1
            self._da_param_changes.append(
                {"seq": event["seq"], "tag": event["tag"], "value": event["value"],
                 "_n": self._da_pc_counter}
            )
        elif etype == "da_obj":
            entry = self._walk_entry(event["walk_id"])
            entry["objects"].append(event)
        elif etype == "da_param":
            entry = self._walk_entry(event["walk_id"])
            entry["params"].append(event)
            self._da_param_changes.append(
                {"seq": event["seq"], "tag": event["tag"], "value": event["value"],
                 "title": event.get("title", ""), "display": event.get("display", ""),
                 "_n": self._da_pc_counter}
            )
        elif etype == "da_done":
            entry = self._walk_entry(event["walk_id"])
            entry["count"] = event["count"]
            entry["done"].set()

    def _walk_entry(self, walk_id: int) -> dict:
        entry = self._da_walks.get(walk_id)
        if entry is None:
            entry = {
                "walk_id": walk_id,
                "gen": self._da_walk_gen,  # attach to the pending request, if any
                "objects": [],
                "params": [],
                "done": threading.Event(),
            }
            self._da_walks[walk_id] = entry
            if len(self._da_walks) > 8:  # keep the map bounded
                for k in sorted(self._da_walks, key=lambda k: self._da_walks[k]["gen"])[:-4]:
                    self._da_walks.pop(k, None)
        return entry

    # ---------------------------------------------------------------- output
    def _send_sysex_data(self, data: List[int]) -> None:
        msg = mido.Message("sysex", data=data)
        if self.backend == "dummy":
            if self._fake is None:
                raise BridgeError("dummy backend not started")
            self._fake.on_message(msg)
            return
        if self._mido_out is None:
            raise BridgeError("MIDI output not open")
        with self._lock:
            self._recent.append({"dir": "out", "sysex_hex": msg.hex()})
        self._mido_out.send(msg)

    def send_cc(self, cc: int, value: int) -> None:
        msg = mido.Message("control_change", channel=P.CC.CHANNEL, control=cc, value=max(0, min(127, int(value))))
        if self.backend == "dummy":
            if self._fake is None:
                raise BridgeError("dummy backend not started")
            self._fake.on_message(msg)
            return
        if self._mido_out is None:
            raise BridgeError("MIDI output not open")
        with self._lock:
            self._recent.append({"dir": "out", "cc": cc, "value": msg.value})
        self._mido_out.send(msg)

    def _press_cc(self, cc: int) -> None:
        self.send_cc(cc, 127)
        time.sleep(0.05)
        self.send_cc(cc, 0)
        time.sleep(0.15)  # let the host reflect the change before callers read state

    # ------------------------------------------------------------ inspection
    def state(self) -> dict:
        self._require_ready()
        with self._lock:
            snap = {
                "backend": self.backend,
                "ident": self._state["ident"],
                "transport": dict(self._state["transport"]),
                "plugin": dict(self._state["plugin"]),
                "slots": [dict(s) for s in self._state["slots"]],
                "qc": list(self._state["qc"]),
            }
        return snap

    def recent(self, n: int = 50) -> List[dict]:
        with self._lock:
            items = list(self._recent)
        return items[-n:]

    def drain_recent(self) -> List[dict]:
        """Atomically return every buffered message and clear the buffer."""
        with self._lock:
            items = list(self._recent)
            self._recent.clear()
        return items

    def wait_for_ident(self, timeout: float = 3.0) -> bool:
        return self._ident_event.wait(timeout)

    # -------------------------------------------------------------- commands
    def ping(self, timeout: float = 3.0) -> dict:
        self._ident_event.clear()
        self._send_sysex_data(P.cmd_ping())
        ok = self.wait_for_ident(timeout)
        return {"ok": ok, "ident": self.state()["ident"]}

    def refresh(self) -> None:
        self._send_sysex_data(P.cmd_snapshot())

    def set_volume(self, slot: int, value01: float) -> None:
        self._send_sysex_data(P.cmd_set_volume(slot, round(value01 * 16383)))

    def set_pan(self, slot: int, value01: float) -> None:
        self._send_sysex_data(P.cmd_set_pan(slot, round(value01 * 16383)))

    def set_send(self, slot: int, send_index: int, value01: float) -> None:
        self._send_sysex_data(P.cmd_set_send(slot, send_index, round(value01 * 16383)))

    def set_toggle_param(self, slot: int, param: str, on: bool) -> None:
        """Toggle mute/solo/record_arm/monitor via a real CC press.

        Toggle bindings respond to the press EDGE, and host feedback keeps the
        surface value latched — so the reliable path is a real CC press/release
        gated by the cached state (no-op when already in the desired state).
        """
        cc = P.CC.slot_param_cc(param, slot)
        if cc is None:
            raise BridgeError(f"invalid param/slot: {param}/{slot}")
        if bool(self.state()["slots"][slot].get(param)) is bool(on):
            return
        self._press_cc(cc)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if bool(self.state()["slots"][slot].get(param)) is bool(on):
                return
            time.sleep(0.1)

    def select_slot(self, slot: int, on: bool = True) -> None:
        cc = P.CC.select_base + slot
        if bool(self.state()["slots"][slot]["selected"]) is bool(on):
            return
        self._press_cc(cc)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if bool(self.state()["slots"][slot]["selected"]) is bool(on):
                return
            time.sleep(0.1)

    def quick_control(self, index: int, value01: float) -> None:
        self._send_sysex_data(P.cmd_set_qc(index, round(value01 * 16383)))

    def transport(self, action: str) -> None:
        cc = TRANSPORT_ACTIONS.get(action)
        if cc is None:
            raise BridgeError(f"action must be one of {sorted(TRANSPORT_ACTIONS)}")
        self._press_cc(cc)

    def locate(self, position: str) -> None:
        self._send_sysex_data(P.cmd_locate(position))

    def set_tempo(self, bpm: float) -> None:
        self._send_sysex_data(P.cmd_set_tempo(round(bpm * 10)))

    def bank(self, action: str) -> None:
        step = BANK_ACTIONS.get(action)
        if step is None:
            raise BridgeError(f"action must be one of {sorted(BANK_ACTIONS)}")
        self._send_sysex_data(P.cmd_bank(step))
        time.sleep(0.12)

    def key_command(self, slot: int) -> None:
        if not (0 <= slot < P.CC.NUM_KC):
            raise BridgeError(f"slot must be 0..{P.CC.NUM_KC - 1}")
        self._send_sysex_data(P.cmd_kc_slot(slot))
        time.sleep(0.05)

    def insert_slot(self, action: str) -> None:
        cc = {"prev": P.CC.fx_slot_prev, "next": P.CC.fx_slot_next}.get(action)
        if cc is None:
            raise BridgeError("action must be 'prev' or 'next'")
        self._press_cc(cc)

    def param_bank(self, action: str) -> None:
        cc = {"prev": P.CC.fx_param_bank_prev, "next": P.CC.fx_param_bank_next}.get(action)
        if cc is None:
            raise BridgeError("action must be 'prev' or 'next'")
        self._press_cc(cc)

    # --------------------------------------------------------- track finding
    def select_track(self, name: str, max_steps: int = 12, settle: float = 0.8) -> dict:
        """Select a track by (substring) name, walking the visible bank window.

        Slot names arrive via the fader surface value's title callback, so the
        visible 8-slot window is searched first; if the name is not found the
        bank is stepped until the window stops changing (or wraps).
        """
        lowered = name.lower()

        def find():
            for s in self.state()["slots"]:
                if s["exists"] and s["name"] and lowered in s["name"].lower():
                    return s
            return None

        def signature():
            return tuple(s["name"] for s in self.state()["slots"])

        target = find()
        steps = 0
        if target is None:
            seen = [signature()]
            for direction in ("next", "prev"):
                seen = [signature()]
                for _ in range(max_steps):
                    self.bank(direction)
                    steps += 1
                    time.sleep(settle)
                    target = find()
                    if target is not None:
                        break
                    sig = signature()
                    if sig in seen:
                        break  # window did not move -> end of bank in this direction
                    seen.append(sig)
                if target is not None:
                    break
        if target is None:
            return {"found": False, "steps": steps,
                    "slots": [s["name"] for s in self.state()["slots"]]}
        self.select_slot(target["slot"])
        return {"found": True, "slot": target["slot"], "track": target["name"],
                "steps": steps}

    # ---------------------------------------------------------- direct access
    def da_walk(self, limit: int = 80, timeout: float = 3.0) -> dict:
        """Request a DirectAccess walk and wait for its completion.

        Responses are buffered per walk_id, so a delayed frame from another
        walk can never pollute this request's result.
        """
        with self._lock:
            self._da_walk_gen += 1
            gen = self._da_walk_gen
        self._send_sysex_data(P.cmd_da_walk(limit))
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for entry in self._da_walks.values():
                    if entry["gen"] == gen and entry["done"].is_set():
                        return {
                            "finished": True,
                            "walk_id": entry["walk_id"],
                            "objects": list(entry["objects"]),
                            "params": list(entry["params"]),
                        }
            time.sleep(0.02)
        with self._lock:
            partial = [e for e in self._da_walks.values() if e["gen"] == gen]
        return {
            "finished": False,
            "walk_id": partial[0]["walk_id"] if partial else None,
            "objects": list(partial[0]["objects"]) if partial else [],
            "params": list(partial[0]["params"]) if partial else [],
        }

    def da_set_param(self, seq: int, tag: int, value01: float) -> dict:
        self._send_sysex_data(P.cmd_da_set(seq, tag, round(value01 * 16383)))
        return {"seq": seq, "tag": tag, "value": value01}

    def da_get_param(self, seq: int, tag: int, timeout: float = 1.0) -> dict:
        with self._cond:
            marker = self._da_pc_counter  # events appended later have _n > marker
        self._send_sysex_data(P.cmd_da_get(seq, tag))
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cond:
                for ch in reversed(self._da_param_changes):
                    if ch["_n"] > marker and ch["seq"] == seq and ch["tag"] == tag:
                        return {k: v for k, v in ch.items() if k != "_n"}
            time.sleep(0.02)
        return {"seq": seq, "tag": tag, "value": None, "timeout": True}

    def query_selected_name(self, timeout: float = 1.5) -> Optional[str]:
        """Query the selected track's name (DirectAccess title via the script)."""
        with self._cond:
            self._selected_name = None  # clear stale value
        self._send_sysex_data(P.cmd_selected_name())
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._cond:
                if self._selected_name is not None:
                    name = self._selected_name
                    self._selected_name = None  # consume
                    return name
            time.sleep(0.05)
        return None


# ---------------------------------------------------------------------------
# Fake Cubase (dummy backend) — emulates the JS script for dry testing
# ---------------------------------------------------------------------------

class _FakeCubase:
    """In-process imitation of ClaudeCode_MCP_Bridge.js responses."""

    TRACKS = [
        "Drums", "Bass", "Keys", "Vox Lead", "Guitar", "FX Reverb",
        "Group Drums", "Group Vox",  # first bank window
        "Vox Double", "Strings", "Perc Loop", "Master Bus",  # second window
    ]

    DA_TREE = [
        {"seq": 0, "parent": 16383, "object_type": "MixerChannel", "title": "Vox Lead",
         "params": [
             {"tag": 11, "title": "Volume", "value": 0.8, "display": "-1.24 dB"},
             {"tag": 12, "title": "Pan", "value": 0.5, "display": "C"},
         ]},
        {"seq": 1, "parent": 0, "object_type": "QuickControls", "title": "Quick Controls",
         "params": [{"tag": 21, "title": "QC 1", "value": 0.5, "display": "0.50"}]},
        {"seq": 2, "parent": 0, "object_type": "InsertEffect", "title": "REVerb 2",
         "params": [
             {"tag": 31, "title": "Dry/Wet", "value": 0.35, "display": "35 %"},
             {"tag": 32, "title": "Size", "value": 0.6, "display": "3.2 s"},
         ]},
    ]

    def __init__(self, emit: Callable[[mido.Message], None]) -> None:
        self.emit = emit
        self.bank_start = 0
        self.bpm10 = 1200
        self.locator = "1.1.1.0"
        self.locator_fmt = "Bars.Beats"
        self.playing = False
        self.selected = 0
        self.track_index = 0
        self.slots = [
            {
                "exists": False, "name": "", "selected": False, "mute": False,
                "solo": False, "record_arm": False, "monitor": False,
                "volume": 0.8, "pan": 0.5,
            }
            for _ in range(NUM_SLOTS)
        ]
        self.qc = [0.5] * NUM_QC
        self._refresh_window()

    # ---------------------------------------------------------------- helpers
    def _step_track(self, delta: int) -> None:
        count = len(self.TRACKS)
        self.track_index = (self.track_index + delta) % count
        self._refresh_window()

    def _refresh_window(self) -> None:
        window = self.TRACKS[self.bank_start:self.bank_start + NUM_SLOTS]
        for i, slot in enumerate(self.slots):
            if i < len(window):
                slot["exists"] = True
                slot["name"] = window[i]
                slot["selected"] = (self.bank_start + i == self.track_index)
            else:
                slot["exists"] = False
                slot["name"] = ""
                slot["selected"] = False

    def _emit(self, data: List[int]) -> None:
        self.emit(mido.Message("sysex", data=data))

    def _ident(self) -> None:
        self._emit([P.SYSEX_VENDOR_ID, P.SX.IDENT] + P.encode_string(P.IDENT_STRING))

    def _transport(self) -> None:
        flags = (
            (0x01 if self.playing else 0)
            | (0x02 if getattr(self, "recording", False) else 0)
            | (0x04 if getattr(self, "cycle", False) else 0)
            | (0x08 if getattr(self, "metro", False) else 0)
        )
        self._emit(
            [P.SYSEX_VENDOR_ID, P.SX.TRANSPORT, flags]
            + list(P.split_u14(self.bpm10))
            + P.encode_flag_string(True, self.locator)
            + P.encode_flag_string(True, self.locator_fmt)
        )

    def _slot(self, i: int) -> None:
        s = self.slots[i]
        flags = 0x20
        if s["selected"]:
            flags |= 0x01
        if s["mute"]:
            flags |= 0x02
        if s["solo"]:
            flags |= 0x04
        if s["record_arm"]:
            flags |= 0x08
        if s["monitor"]:
            flags |= 0x10
        self._emit(
            [P.SYSEX_VENDOR_ID, P.SX.SLOT, i, flags]
            + list(P.split_u14(round(s["volume"] * 16383)))
            + list(P.split_u14(round(s["pan"] * 16383)))
            + P.encode_flag_string(True, s["name"])
        )

    def _snapshot(self) -> None:
        self._ident()
        self._transport()
        for i in range(NUM_SLOTS):
            self._slot(i)
        for i in range(NUM_QC):
            self._emit(
                [P.SYSEX_VENDOR_ID, P.SX.QC, i] + list(P.split_u14(round(self.qc[i] * 16383)))
            )

    def _plugin(self) -> None:
        self._emit(
            [P.SYSEX_VENDOR_ID, P.SX.PLUGIN, 1]
            + P.encode_string("Halion Sonic")
            + P.encode_string("Steinberg")
            + P.encode_string("7.5.0")
        )

    # ---------------------------------------------------------------- handler
    def on_message(self, msg: mido.Message) -> None:
        if msg.type == "control_change":
            self._on_cc(msg.control, msg.value)
        elif msg.type == "sysex":
            self._on_sysex(list(msg.data))

    def _on_cc(self, cc: int, value: int) -> None:
        pressed = value >= 64
        if not pressed:
            return
        # select CC (56..63): select the corresponding slot's track
        if P.CC.select_base <= cc < P.CC.select_base + NUM_SLOTS:
            slot_i = cc - P.CC.select_base

            def _select(i=slot_i):
                gi = self.bank_start + i
                self.track_index = -1 if self.track_index == gi else gi
                self._refresh_window()
                for j in range(NUM_SLOTS):
                    self._slot(j)
            return _select()

        # mute/solo/recarm/monitor CC toggles (CC is slot-param base + slot)
        for param_base, key in ((P.CC.mute_base, "mute"), (P.CC.solo_base, "solo"),
                                (P.CC.recarm_base, "record_arm"), (P.CC.monitor_base, "monitor")):
            if param_base <= cc < param_base + NUM_SLOTS:
                slot_i = cc - param_base
                if self.slots[slot_i]["exists"]:

                    def _toggle(i=slot_i, k=key):
                        self.slots[i][k] = not self.slots[i][k]
                        self._slot(i)
                    return _toggle()
        mapping = {
            P.CC.bank_next: lambda: self._bank("next"),
            P.CC.bank_prev: lambda: self._bank("prev"),
            P.CC.bank_shift_left: lambda: self._bank("shift_left"),
            P.CC.bank_shift_right: lambda: self._bank("shift_right"),
            P.CC.bank_reset: lambda: self._bank("reset"),
            P.CC.track_next: lambda: self._step_track(1),
            P.CC.track_prev: lambda: self._step_track(-1),
            P.CC.play: lambda: setattr(self, "playing", not self.playing),
            P.CC.stop: lambda: setattr(self, "playing", False),
            P.CC.record: lambda: setattr(self, "recording", not getattr(self, "recording", False)),
            P.CC.cycle: lambda: setattr(self, "cycle", not getattr(self, "cycle", False)),
            P.CC.metronome: lambda: setattr(self, "metro", not getattr(self, "metro", False)),
        }
        fn = mapping.get(cc)
        if fn:
            fn()
            self._transport()
            for i in range(NUM_SLOTS):
                self._slot(i)

    def _bank(self, action: str) -> None:
        max_start = max(0, len(self.TRACKS) - NUM_SLOTS)
        if action == "next":
            self.bank_start = min(self.bank_start + NUM_SLOTS, max_start)
        elif action == "prev":
            self.bank_start = max(self.bank_start - NUM_SLOTS, 0)
        elif action == "shift_left":
            self.bank_start = max(self.bank_start - 1, 0)
        elif action == "shift_right":
            self.bank_start = min(self.bank_start + 1, max_start)
        elif action == "reset":
            self.bank_start = 0
        self._refresh_window()

    def _on_sysex(self, data: List[int]) -> None:
        if len(data) < 2 or data[0] != P.SYSEX_VENDOR_ID:
            return
        cmd = data[1]
        body = data[2:]

        if cmd == P.SX.PING:
            self._snapshot()
            self._plugin()
        elif cmd == P.SX.SNAPSHOT:
            self._snapshot()
        elif cmd == P.SX.SELECT_SLOT:
            slot = body[0]
            self.track_index = self.bank_start + slot
            self._refresh_window()
            for i in range(NUM_SLOTS):
                self._slot(i)
        elif cmd == P.SX.DESELECT_SLOT:
            slot = body[0]
            if self.bank_start + slot == self.track_index:
                self.track_index = -1
            self._refresh_window()
            for i in range(NUM_SLOTS):
                self._slot(i)
        elif cmd == P.SX.SELECTED_NAME:
            # selected track's name (mirrors the real script's DA title read)
            name = self.TRACKS[self.track_index] if 0 <= self.track_index < len(self.TRACKS) else ""
            self._emit(
                [P.SYSEX_VENDOR_ID, P.SX.SELECTED_NAME_OUT] + P.encode_string(name)
            )
        elif cmd == P.SX.SET_VOLUME:
            slot = body[0]
            self.slots[slot]["volume"] = P.join_u14(body[1], body[2]) / 16383.0
            self._slot(slot)
        elif cmd == P.SX.SET_PAN:
            slot = body[0]
            self.slots[slot]["pan"] = P.join_u14(body[1], body[2]) / 16383.0
            self._slot(slot)
        elif cmd in (P.SX.SET_MUTE, P.SX.SET_SOLO, P.SX.SET_RECARM, P.SX.SET_MONITOR):
            slot = body[0]
            on = bool(body[1])
            key = {
                P.SX.SET_MUTE: "mute", P.SX.SET_SOLO: "solo",
                P.SX.SET_RECARM: "record_arm", P.SX.SET_MONITOR: "monitor",
            }[cmd]
            self.slots[slot][key] = on
            self._slot(slot)
        elif cmd == P.SX.SET_SEND:
            pass  # sends are not mirrored in the fake
        elif cmd == P.SX.SET_QC:
            idx = body[0]
            self.qc[idx] = P.join_u14(body[1], body[2]) / 16383.0
            self._emit(
                [P.SYSEX_VENDOR_ID, P.SX.QC, idx] + list(P.split_u14(round(self.qc[idx] * 16383)))
            )
        elif cmd == P.SX.LOCATE:
            self.locator, _ = P.decode_string(body, 0)
            self._transport()
        elif cmd == P.SX.SET_TEMPO:
            self.bpm10 = P.join_u14(body[0], body[1])
            self._transport()
        elif cmd == P.SX.BANK:
            self._bank({0: "next", 1: "prev", 2: "shift_left", 3: "shift_right", 4: "reset"}.get(
                body[0], "reset"
            ))
            self._transport()
            for i in range(NUM_SLOTS):
                self._slot(i)
        elif cmd == P.SX.DA_WALK:
            limit = P.join_u14(body[0], body[1]) or 80
            walk_id = 7
            count = 0
            for obj in self.DA_TREE[: max(1, limit // 4)]:
                payload = (
                    [P.SYSEX_VENDOR_ID, P.SX.DA_OBJ, walk_id]
                    + list(P.split_u14(obj["seq"]))
                    + list(P.split_u14(obj["parent"]))
                    + P.encode_flag_string(True, obj["object_type"])
                    + P.encode_flag_string(True, obj["title"])
                )
                self._emit(payload)
                count += 1
                for param in obj["params"]:
                    payload = (
                        [P.SYSEX_VENDOR_ID, P.SX.DA_PARAM, walk_id]
                        + list(P.split_u14(obj["seq"]))
                        + list(P.split_u21(param["tag"]))
                        + list(P.split_u14(round(param["value"] * 16383)))
                        + P.encode_flag_string(True, param["title"])
                        + P.encode_flag_string(True, param["display"])
                    )
                    self._emit(payload)
                    count += 1
            self._emit(
                [P.SYSEX_VENDOR_ID, P.SX.DA_DONE, walk_id]
                + list(P.split_u14(count))
            )
        elif cmd == P.SX.DA_SET:
            seq = P.join_u14(body[0], body[1])
            tag = P.join_u21(body[2], body[3], body[4])
            value = P.join_u14(body[5], body[6]) / 16383.0
            param = self._find_da_param(seq, tag)
            if param is not None:
                param["value"] = value
            # mirror what the real script's mOnParameterChange would push
            self._emit(
                [P.SYSEX_VENDOR_ID, P.SX.DA_PARAM_CHANGE]
                + list(P.split_u14(seq))
                + list(P.split_u21(tag))
                + list(P.split_u14(round(value * 16383)))
            )
        elif cmd == P.SX.DA_GET:
            seq = P.join_u14(body[0], body[1])
            tag = P.join_u21(body[2], body[3], body[4])
            param = self._find_da_param(seq, tag)
            if param is None:
                return  # mirror the script: stay silent for unknown seq/tag
            self._emit(
                [P.SYSEX_VENDOR_ID, P.SX.DA_PARAM_CHANGE]
                + list(P.split_u14(seq))
                + list(P.split_u21(tag))
                + list(P.split_u14(round(param["value"] * 16383)))
            )

    def _find_da_param(self, seq: int, tag: int) -> Optional[dict]:
        for obj in self.DA_TREE:
            if obj["seq"] == seq:
                for param in obj["params"]:
                    if param["tag"] == tag:
                        return param
        return None
