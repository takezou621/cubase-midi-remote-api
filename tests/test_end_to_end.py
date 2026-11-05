"""End-to-end tests against the fake Cubase responder (dummy backend).

Run:  ./.venv/bin/python tests/test_end_to_end.py
No Cubase or MIDI hardware required.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CUBASE_MIDI_BACKEND", "dummy")

from mcp_server.bridge import MidiBridge  # noqa: E402


def main() -> int:
    bridge = MidiBridge(backend="dummy")
    bridge.start()
    failures = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
        if not condition:
            failures.append(name)

    # -- ident / ping -------------------------------------------------------
    r = bridge.ping()
    check("ping identifies script", r["ok"] and "MCP_Bridge" in (r["ident"] or ""))

    # -- state --------------------------------------------------------------
    state = bridge.state()
    check("first bank shows Drums", state["slots"][0]["name"] == "Drums",
          state["slots"][0]["name"])

    # -- volume -------------------------------------------------------------
    bridge.set_volume(0, 0.25)
    vol = bridge.state()["slots"][0]["volume"]
    check("set volume slot 0", abs(vol - 0.25) < 0.01, f"{vol:.3f}")

    # -- pan ----------------------------------------------------------------
    bridge.set_pan(1, 0.75)
    pan = bridge.state()["slots"][1]["pan"]
    check("set pan slot 1", abs(pan - 0.75) < 0.01, f"{pan:.3f}")

    # -- mute toggle via sysex ----------------------------------------------
    bridge.set_toggle_param(2, "mute", True)
    check("mute slot 2", bridge.state()["slots"][2]["mute"] is True)
    bridge.set_toggle_param(2, "mute", False)
    check("unmute slot 2", bridge.state()["slots"][2]["mute"] is False)

    # -- quick control ------------------------------------------------------
    bridge.quick_control(3, 0.9)
    check("quick control 3", abs(bridge.state()["qc"][3] - 0.9) < 0.01)

    # -- transport via CC ---------------------------------------------------
    bridge.transport("play")
    check("transport play", bridge.state()["transport"]["play"] is True)
    bridge.transport("stop")
    check("transport stop", bridge.state()["transport"]["play"] is False)

    # -- locate / tempo -----------------------------------------------------
    bridge.locate("17.3.2.1")
    check("locate", bridge.state()["transport"]["locator"] == "17.3.2.1")
    bridge.set_tempo(132.5)
    check("tempo 132.5", abs(bridge.state()["transport"]["tempo_bpm"] - 132.5) < 0.01)

    # -- bank / select track ------------------------------------------------
    bridge.bank("next")
    names = [s["name"] for s in bridge.state()["slots"]]
    check("bank next shows Strings", "Strings" in names, str(names))
    r = bridge.select_track("Vox Lead")
    check("select_track finds Vox Lead", r["found"] and r["track"] == "Vox Lead", str(r))
    check("Vox Lead now selected", bridge.state()["slots"][r["slot"]]["selected"] is True)

    # -- Japanese track names survive the round trip -------------------------
    bridge._fake.TRACKS[1] = "バイオリン編成A"  # noqa: SLF001 — test hook
    bridge._fake._refresh_window()
    r = bridge.select_track("バイオリン")
    check("Japanese name round trip", r["found"] and "バイオリン" in r["track"], str(r))

    # -- direct access -------------------------------------------------------
    da = bridge.da_walk()
    check("da_walk finished", da["finished"] and len(da["objects"]) >= 3)
    titles = [o["title"] for o in da["objects"]]
    check("da_walk sees insert effect", any("REVerb" in t for t in titles), str(titles))
    got = bridge.da_get_param(2, 31)
    check("da_get_param Dry/Wet", abs(got["value"] - 0.35) < 0.01, f"{got['value']:.3f}")
    bridge.da_set_param(2, 31, 0.5)
    got = bridge.da_get_param(2, 31)
    check("da_set_param applies (fake mirrors)", got["value"] is not None and abs(got["value"] - 0.5) < 0.01,
          f"{got['value']}")

    # -- deselect -------------------------------------------------------------
    sel = bridge.state()["slots"][r["slot"]]
    bridge.select_slot(r["slot"], on=False)
    check("deselect slot", bridge.state()["slots"][r["slot"]]["selected"] is False,
          sel["name"])

    # -- plugin identity ------------------------------------------------------
    ident = bridge.state()["plugin"]
    check("plugin identity", "Halion" in ident["name"], ident["name"])

    bridge.stop()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
