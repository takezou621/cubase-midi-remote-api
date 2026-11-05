"""JS <-> Python protocol conformance test.

Runs the actual MIDI Remote script in a Node mock (tests/js_mock_test.cjs),
captures every outbound SysEx frame, and verifies that the Python-side
parser (mcp_server.protocol.parse_feedback) decodes them correctly.

This is the regression test for the feedback-frame layout (e.g. a duplicated
command byte would make every SLOT/TRANSPORT frame unparsable).

Run:  ./.venv/bin/python tests/test_js_protocol.py   (requires Node.js)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "cubase_script", "Local", "ClaudeCode", "MCP_Bridge", "ClaudeCode_MCP_Bridge.js")

sys.path.insert(0, ROOT)

from mcp_server import protocol as P  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def main() -> int:
    if not os.path.exists(SCRIPT):
        print(f"script not found: {SCRIPT}")
        return 2

    try:
        proc = subprocess.run(
            ["node", os.path.join(HERE, "js_mock_test.cjs"), SCRIPT],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except FileNotFoundError:
        print("SKIP: Node.js not installed — JS protocol conformance test skipped")
        return 0
    except subprocess.CalledProcessError as exc:
        print(exc.stderr[-2000:])
        return 1

    data = json.loads(proc.stdout)
    frames = data["frames"]
    check("script produced frames", len(frames) > 0, f"{len(frames)} frames")

    events = []
    for i, frame in enumerate(frames):
        if not (frame[0] == 0xF0 and frame[-1] == 0xF7):
            check(f"frame {i} wrapped in F0/F7", False, str(frame[:6]))
            continue
        ev = P.parse_feedback(list(frame[1:-1]))
        if ev is None or ev["type"] == "unknown":
            check(f"frame {i} parsed as a known event", False,
                  f"cmd=0x{frame[2]:02x} bytes={[hex(b) for b in frame[:8]]}")
            continue
        events.append(ev)

    check("all frames parse into known events", len(events) == len(frames),
          f"{len(events)}/{len(frames)}")

    idents = [e for e in events if e["type"] == "ident"]
    check("PING answered with IDENT", any("MCP_Bridge" in e["text"] for e in idents),
          idents[0]["text"] if idents else "none")

    slots = {e["slot"]: e for e in events if e["type"] == "slot"}
    check("8 slot frames in snapshot", len(slots) == 8, str(sorted(slots)))
    check("slot 0 carries track name", slots.get(0, {}).get("name") == "Drums",
          slots.get(0, {}).get("name", ""))
    check("slot 0 carries volume feedback", abs(slots.get(0, {}).get("volume", 0) - 0.5) < 0.01,
          f"{slots.get(0, {}).get('volume')}")
    check("slot 1 carries track name", slots.get(1, {}).get("name") == "Bass",
          slots.get(1, {}).get("name", ""))
    check("slot 0 exists flag set", slots.get(0, {}).get("exists") is True)

    transports = [e for e in events if e["type"] == "transport"]
    check("transport frame parses", len(transports) >= 1 and "locator" in transports[0],
          transports[0]["locator_format"] if transports else "none")

    qcs = [e for e in events if e["type"] == "qc"]
    check("8 QC frames in snapshot", len(qcs) == 8)

    plugins = [e for e in events if e["type"] == "plugin"]
    check("plugin frame parses", len(plugins) >= 1 and "role" in plugins[0])

    # SET_VOLUME slot 2 must have pushed 0.5 into the fader surface value
    sp = [c for c in data["setProcessValue"]
          if c["node"] == "makeFader#2" and abs(c["value"] - 0.5) < 0.001]
    check("SET_VOLUME sysex reached fader surface value", len(sp) >= 1, str(data["setProcessValue"]))

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all JS<->Python protocol checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
