"""Command line helpers: list MIDI ports, monitor traffic, send test commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .bridge import BridgeError, MidiBridge


def _make_bridge(args) -> MidiBridge:
    return MidiBridge(
        in_port=getattr(args, "in_port", None) or os.environ.get("CUBASE_MIDI_IN"),
        out_port=getattr(args, "out_port", None) or os.environ.get("CUBASE_MIDI_OUT"),
        backend=getattr(args, "backend", None) or os.environ.get("CUBASE_MIDI_BACKEND", "rtmidi"),
    )


def cmd_list_ports(_args) -> int:
    import mido

    print("MIDI inputs:")
    for name in mido.get_input_names():
        print(f"  [in ] {name}")
    print("MIDI outputs:")
    for name in mido.get_output_names():
        print(f"  [out] {name}")
    print(
        "\nHint: enable 'IAC Driver Bus 1' in Audio MIDI Setup (macOS) or install"
        "\nloopMIDI (Windows) so Cubase and this bridge share a virtual cable."
    )
    return 0


def cmd_monitor(args) -> int:
    try:
        bridge = _make_bridge(args)
        bridge.start()
    except BridgeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Monitoring {bridge.backend} backend... Ctrl-C to stop")
    try:
        while True:
            items = bridge.drain_recent()
            for item in items:
                print(json.dumps(item, ensure_ascii=False))
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


def cmd_test(args) -> int:
    try:
        bridge = _make_bridge(args)
        bridge.start()
    except BridgeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    ok = True
    print("== ping ==")
    result = bridge.ping()
    print(json.dumps(result, ensure_ascii=False))
    ok &= result.get("ok", False)
    if not ok and bridge.backend == "rtmidi":
        print("No answer from the script. Check: script installed, MIDI Remote panel"
              "\nopen (Remote tab), ports matched (see README).")
        bridge.stop()
        return 1
    print("== state ==")
    print(json.dumps(bridge.state(), ensure_ascii=False, indent=2))
    print("== set volume slot 0 -> 0.5 ==")
    bridge.set_volume(0, 0.5)
    time.sleep(0.2)
    print(json.dumps(bridge.state()["slots"][0], ensure_ascii=False))
    print("== bank next ==")
    bridge.bank("next")
    print(json.dumps([s["name"] for s in bridge.state()["slots"]], ensure_ascii=False))
    print("== select track 'Vox Lead' ==")
    print(json.dumps(bridge.select_track("Vox Lead"), ensure_ascii=False))
    print("== da walk ==")
    print(json.dumps(bridge.da_walk(), ensure_ascii=False)[:2000])
    bridge.stop()
    print("== done ==")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_server", description="Cubase MIDI bridge helpers")
    sub = parser.add_subparsers(dest="command")

    def add_common(p):
        p.add_argument("--in-port", help="MIDI input port name (substring match)")
        p.add_argument("--out-port", help="MIDI output port name (substring match)")
        p.add_argument("--backend", choices=["rtmidi", "dummy"], help="MIDI backend")

    sub.add_parser("list-ports", help="list system MIDI ports")
    p_monitor = sub.add_parser("monitor", help="print raw bridge MIDI traffic")
    add_common(p_monitor)
    p_test = sub.add_parser("test", help="run a small end-to-end test sequence")
    add_common(p_test)

    args = parser.parse_args(argv)
    if args.command == "list-ports":
        return cmd_list_ports(args)
    if args.command == "monitor":
        return cmd_monitor(args)
    if args.command == "test":
        return cmd_test(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
