"""MCP server exposing Cubase control tools over the MIDI Remote bridge.

Run with:  python -m mcp_server          (MCP stdio server)
Config env vars:
  CUBASE_MIDI_IN      input port name (default: auto-detect IAC/loopMIDI)
  CUBASE_MIDI_OUT     output port name
  CUBASE_MIDI_BACKEND rtmidi (default) | dummy  (dummy = no real MIDI, for testing)
"""

from __future__ import annotations

import atexit
import logging
import os
import sys

from mcp.server.mcpserver import MCPServer

from .bridge import BridgeError, MidiBridge

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("cubase_mcp.server")

mcp = MCPServer(
    "cubase",
    instructions=(
        "Control the Cubase/Nuendo mix console and transport through a MIDI Remote "
        "API bridge. Read cubase_state() first to see which tracks are visible in "
        "the current bank window (8 slots). Use cubase_select_track(name) to find "
        "and select tracks by name. DirectAccess tools need Cubase 13.0.50+."
    ),
)

bridge = MidiBridge(
    in_port=os.environ.get("CUBASE_MIDI_IN"),
    out_port=os.environ.get("CUBASE_MIDI_OUT"),
    backend=os.environ.get("CUBASE_MIDI_BACKEND", "rtmidi"),
)
atexit.register(bridge.stop)


def _guard(fn):
    """Run fn, converting bridge errors into tool result dicts."""
    try:
        return fn()
    except BridgeError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("tool failed")
        return {"error": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ status

@mcp.tool()
def cubase_ping() -> dict:
    """Check the bridge: virtual MIDI ports open and the MIDI Remote script answers."""
    return _guard(lambda: bridge.ping())


@mcp.tool()
def cubase_state() -> dict:
    """Read the current bridge state: transport, the 8 visible bank slots with
    track names, volume/pan/mute/solo, Quick Controls and the focused plugin."""
    return _guard(bridge.state)


@mcp.tool()
def cubase_monitor(seconds: float = 2.0) -> dict:
    """Debug helper: collect raw MIDI traffic (both directions) for N seconds."""
    def run():
        import time as _time
        bridge.refresh()
        _time.sleep(min(max(seconds, 0.1), 10.0))
        return {"backend": bridge.backend, "messages": bridge.recent(200)}
    return _guard(run)


# --------------------------------------------------------------- transport

@mcp.tool()
def cubase_transport(action: str) -> dict:
    """Transport control. action: play | stop | record | cycle | metronome | rewind | forward."""
    def run():
        bridge.transport(action)
        return {"action": action, "transport": bridge.state()["transport"]}
    return _guard(run)


@mcp.tool()
def cubase_locate(position: str) -> dict:
    """Move the play cursor. Position uses the project's primary time display
    format, e.g. '2.3.1.0' (bars.beats) or '00:01:15:00' (time)."""
    def run():
        bridge.locate(position)
        return {"located_to": position, "transport": bridge.state()["transport"]}
    return _guard(run)


@mcp.tool()
def cubase_set_tempo(bpm: float) -> dict:
    """Set the project tempo in BPM (30..300)."""
    def run():
        if not (30 <= bpm <= 300):
            return {"error": "bpm must be between 30 and 300"}
        bridge.set_tempo(bpm)
        return {"tempo_bpm": bpm}
    return _guard(run)


# ---------------------------------------------------------------- channels

@mcp.tool()
def cubase_set_channel(slot: int, param: str, value) -> dict:
    """Set a parameter of one of the 8 visible bank slots.
    param: volume (0..1) | pan (0..1, 0.5=center) | mute | solo | record_arm | monitor | select (bool).
    Volume/pan are set with 14-bit precision via SysEx."""
    def run():
        if not (0 <= slot < 8):
            return {"error": "slot must be 0..7 (the visible bank window)"}
        if param in ("volume", "pan"):
            v = float(value)
            if not (0.0 <= v <= 1.0):
                return {"error": "volume/pan take 0..1 (pan 0.5 = center)"}
            bridge.set_volume(slot, v) if param == "volume" else bridge.set_pan(slot, v)
        elif param in ("mute", "solo", "record_arm", "monitor", "select"):
            if param == "select":
                if value:
                    bridge.select_slot(slot)
                else:
                    bridge.select_slot(slot, on=False)
            else:
                bridge.set_toggle_param(slot, param, bool(value))
        else:
            return {"error": "unknown param: " + param}
        return {"slot": slot, "param": param, "value": value,
                "slot_state": bridge.state()["slots"][slot]}
    return _guard(run)


@mcp.tool()
def cubase_set_send(slot: int, send_index: int, value: float) -> dict:
    """Set a send level (send_index 0-based, 0..1) of one of the 8 visible slots. value 0..1."""
    def run():
        if not (0 <= slot < 8):
            return {"error": "slot must be 0..7"}
        if not (0 <= send_index < 2):
            return {"error": "send_index must be 0 or 1 (CC-exposed sends)"}
        if not (0.0 <= value <= 1.0):
            return {"error": "value must be 0..1"}
        bridge.set_send(slot, send_index, value)
        return {"slot": slot, "send_index": send_index, "value": value}
    return _guard(run)


@mcp.tool()
def cubase_quick_control(index: int, value: float) -> dict:
    """Set Quick Control `index` (0..7) of the selected track. value 0..1."""
    def run():
        if not (0 <= index < 8):
            return {"error": "index must be 0..7"}
        if not (0.0 <= value <= 1.0):
            return {"error": "value must be 0..1"}
        bridge.quick_control(index, value)
        return {"index": index, "value": value}
    return _guard(run)


@mcp.tool()
def cubase_bank(action: str) -> dict:
    """Shift the visible 8-channel bank window. action: next | prev | shift_left | shift_right | reset.
    After calling, read cubase_state() to see the new track names."""
    def run():
        bridge.bank(action)
        slots = bridge.state()["slots"]
        return {"action": action, "slots": slots}
    return _guard(run)


@mcp.tool()
def cubase_select_track(name: str) -> dict:
    """Find a track by (sub)string name across bank windows and select it.
    Walks the bank window automatically; may take a few seconds."""
    def run():
        return bridge.select_track(name)
    return _guard(run)


# ------------------------------------------------------- plugin access

@mcp.tool()
def cubase_insert_slot(action: str) -> dict:
    """Change which insert slot the focused-FX parameter knobs address. action: prev | next."""
    def run():
        bridge.insert_slot(action)
        return {"action": action, "plugin": bridge.state()["plugin"]}
    return _guard(run)


@mcp.tool()
def cubase_param_bank(action: str) -> dict:
    """Page the parameter bank of the focused insert FX (8 knobs per page). action: prev | next."""
    def run():
        bridge.param_bank(action)
        return {"action": action}
    return _guard(run)


@mcp.tool()
def cubase_key_command(slot: int) -> dict:
    """Fire key command slot 0..5. The commands are defined in the KC_SLOTS table
    inside the Cubase script ClaudeCode_MCP_Bridge.js (defaults: Add Instrument Track,
    Add Audio Track, Undo, Redo, Transport Start, Transport Stop)."""
    def run():
        bridge.key_command(slot)
        return {"slot": slot, "triggered": True}
    return _guard(run)


# ------------------------------------------------------------ direct access

@mcp.tool()
def cubase_da_walk(limit: int = 80) -> dict:
    """Enumerate the selected track's parameter tree via DirectAccess (Cubase 13.0.50+).
    Returns objects and parameters with (seq, tag) keys usable in da_set_param/da_get_param."""
    def run():
        result = bridge.da_walk(limit=max(10, min(limit, 400)))
        if not result["finished"]:
            return {"error": "walk timed out", **result,
                    "hint": "DirectAccess needs Cubase/Nuendo 13.0.50+ and a selected track"}
        return result
    return _guard(run)


@mcp.tool()
def cubase_da_set_param(seq: int, param_tag: int, value: float) -> dict:
    """Write a DirectAccess parameter found by cubase_da_walk. value is normalized 0..1."""
    def run():
        if not (0.0 <= value <= 1.0):
            return {"error": "value must be 0..1"}
        return bridge.da_set_param(seq, param_tag, value)
    return _guard(run)


@mcp.tool()
def cubase_da_get_param(seq: int, param_tag: int) -> dict:
    """Read a DirectAccess parameter value (normalized 0..1) found by cubase_da_walk."""
    def run():
        return bridge.da_get_param(seq, param_tag)
    return _guard(run)


# ---------------------------------------------------------------- escape hatch

@mcp.tool()
def cubase_raw_sysex(payload_hex: str) -> dict:
    """Send a raw 0x7D SysEx command (advanced). payload_hex excludes F0/F7 and the
    0x7D id, e.g. '03 00 20 00' = SET_VOLUME slot 0 to mid (0x20 = 8192). See PROTOCOL.md."""
    def run():
        try:
            payload = [int(x, 16) for x in payload_hex.replace(",", " ").split()]
        except ValueError:
            return {"error": "payload must be hex bytes like '10 00 40 00'"}
        if any(b > 0x7F for b in payload):
            return {"error": "each byte must be 0x00..0x7F"}
        from .protocol import SYSEX_VENDOR_ID, sysex_data
        cmd = payload[0]
        bridge._send_sysex_data(sysex_data(cmd, payload[1:]))
        return {"sent": payload_hex}
    return _guard(run)


def main() -> None:
    try:
        bridge.start()
        log.info("cubase MCP bridge starting (backend=%s)", bridge.backend)
    except BridgeError as exc:
        # Degraded start: keep the MCP server alive so tool calls return
        # actionable errors instead of crashing the whole server.
        bridge.mark_degraded(str(exc))
        log.warning("MIDI ports unavailable (%s). Tools will report this error "
                    "until the virtual MIDI port (IAC/loopMIDI) is enabled and "
                    "the server restarts.", exc)
    mcp.run()


if __name__ == "__main__":
    main()
