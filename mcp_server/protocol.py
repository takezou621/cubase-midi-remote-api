"""Protocol definitions shared by the MIDI bridge and the fake-Cubase test backend.

Implements PROTOCOL.md: CC map and the 0x7D SysEx namespace.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

SYSEX_VENDOR_ID = 0x7D
IDENT_STRING = "ClaudeCode MCP_Bridge v1"
STR_MAX_UNITS = 32


class SX:
    """SysEx command ids (payload byte after 0x7D).

    NOTE: command ids ride as sysex data bytes, so every id must be <= 0x7F.
    Layout: 0x01-0x1F = MCP -> Cubase commands, 0x20-0x3F = Cubase -> MCP feedback.
    """

    # MCP -> Cubase
    SELECT_SLOT = 0x01
    KC_SLOT = 0x02
    SET_VOLUME = 0x03
    SET_PAN = 0x04
    SET_SEND = 0x05
    SET_MUTE = 0x06
    SET_SOLO = 0x07
    SET_RECARM = 0x08
    SET_MONITOR = 0x09
    SET_QC = 0x0A
    LOCATE = 0x0B
    SET_TEMPO = 0x0C
    DA_WALK = 0x0D
    DA_SET = 0x0E
    DA_GET = 0x0F
    BANK = 0x10
    PING = 0x11
    SNAPSHOT = 0x12

    # Cubase -> MCP
    TRANSPORT = 0x20
    SLOT = 0x21
    QC = 0x22
    PLUGIN = 0x23
    DA_PARAM_CHANGE = 0x24
    DA_OBJ = 0x25
    DA_PARAM = 0x26
    DA_DONE = 0x27
    IDENT = 0x6E


class CC:
    """Control Change map (MIDI channel 0)."""

    CHANNEL = 0
    NUM_CHANNELS = 8
    NUM_SENDS = 2
    NUM_QC = 8
    NUM_FX_KNOBS = 8
    NUM_KC = 6

    volume_base = 0       # 0..7
    bank_prev = 8
    bank_next = 9
    bank_shift_left = 10
    bank_shift_right = 11
    bank_reset = 12
    track_prev = 13
    track_next = 14
    pan_base = 16         # 16..23
    mute_base = 24        # 24..31
    solo_base = 32        # 32..39
    recarm_base = 40      # 40..47
    monitor_base = 48     # 48..55
    select_base = 56      # 56..63
    send_base = (64, 72)  # send 0, send 1
    fx_slot_prev = 80
    fx_slot_next = 81
    fx_param_bank_prev = 82
    fx_param_bank_next = 83
    play = 89
    stop = 90
    record = 91
    cycle = 92
    metronome = 93
    rewind = 94
    forward = 95
    qc_base = 96          # 96..103
    fx_base = 104         # 104..111
    kc_base = 112         # 112..117

    @classmethod
    def slot_param_cc(cls, param: str, slot: int) -> Optional[int]:
        base = {
            "volume": cls.volume_base,
            "pan": cls.pan_base,
            "mute": cls.mute_base,
            "solo": cls.solo_base,
            "record_arm": cls.recarm_base,
            "monitor": cls.monitor_base,
            "select": cls.select_base,
        }.get(param)
        if base is None or not (0 <= slot < cls.NUM_CHANNELS):
            return None
        return base + slot


# ---------------------------------------------------------------------------
# 7-bit / 14-bit / 21-bit helpers
# ---------------------------------------------------------------------------

def split_u14(value: int) -> Tuple[int, int]:
    v = max(0, min(16383, int(round(value))))
    return ((v >> 7) & 0x7F, v & 0x7F)


def join_u14(hi: int, lo: int) -> int:
    return ((hi & 0x7F) << 7) | (lo & 0x7F)


def split_u21(value: int) -> Tuple[int, int, int]:
    v = max(0, int(value))
    return ((v >> 14) & 0x7F, (v >> 7) & 0x7F, v & 0x7F)


def join_u21(a: int, b: int, c: int) -> int:
    return ((a & 0x7F) << 14) | ((b & 0x7F) << 7) | (c & 0x7F)


# ---------------------------------------------------------------------------
# String encoding: [mode][unit_len][data...]
#   mode 0: ASCII, 1 byte per unit
#   mode 1: UTF-16 code units, 21-bit triple per unit (lossless incl. Japanese)
# ---------------------------------------------------------------------------

def encode_string(text: str, max_units: int = STR_MAX_UNITS) -> List[int]:
    s = str(text if text is not None else "")
    units = s[:max_units]
    if all(ord(ch) <= 0x7F for ch in units):
        out: List[int] = [0, len(units)]
        out.extend(ord(ch) & 0x7F for ch in units)
        return out
    out = [1, len(units)]
    for ch in units:
        out.extend(split_u21(ord(ch)))
    return out


def _utf16_units_to_str(units: List[int]) -> str:
    out: List[str] = []
    i = 0
    while i < len(units):
        u = units[i]
        if 0xD800 <= u <= 0xDBFF and i + 1 < len(units) and 0xDC00 <= units[i + 1] <= 0xDFFF:
            code = 0x10000 + ((u - 0xD800) << 10) + (units[i + 1] - 0xDC00)
            out.append(chr(code))
            i += 2
        else:
            out.append(chr(u))
            i += 1
    return "".join(out)


def decode_string(data: List[int], idx: int) -> Tuple[str, int]:
    mode = data[idx]
    units = data[idx + 1]
    pos = idx + 2
    if mode == 0:
        units = max(0, min(units, len(data) - pos))
        chars = [chr(data[pos + i] & 0x7F) for i in range(units)]
        return "".join(chars), pos + units
    units = max(0, min(units, (len(data) - pos) // 3))
    raw = [join_u21(data[pos + i * 3], data[pos + i * 3 + 1], data[pos + i * 3 + 2])
           for i in range(units)]
    return _utf16_units_to_str(raw), pos + units * 3


def decode_flag_string(data: List[int], idx: int) -> Tuple[int, str, int]:
    """Read a [flag][mode][len][data...] field as written by the script's
    pushFlagString / Python's encode_flag_string."""
    flag = data[idx]
    text, next_idx = decode_string(data, idx + 1)
    return flag, text, next_idx


def encode_flag_string(flag: int, text: str) -> List[int]:
    return [1 if flag else 0] + encode_string(text)


# ---------------------------------------------------------------------------
# Command builders (data list for mido sysex message, without 0xF0/0xF7)
# ---------------------------------------------------------------------------

def sysex_data(cmd: int, payload: Optional[List[int]] = None) -> List[int]:
    return [SYSEX_VENDOR_ID, cmd] + list(payload or [])


def cmd_ping() -> List[int]:
    return sysex_data(SX.PING)


def cmd_snapshot() -> List[int]:
    return sysex_data(SX.SNAPSHOT)


def cmd_select_slot(slot: int) -> List[int]:
    return sysex_data(SX.SELECT_SLOT, [slot])


def cmd_kc_slot(slot: int) -> List[int]:
    return sysex_data(SX.KC_SLOT, [slot])


def cmd_set_volume(slot: int, value: int) -> List[int]:
    return sysex_data(SX.SET_VOLUME, [slot] + list(split_u14(value)))


def cmd_set_pan(slot: int, value: int) -> List[int]:
    return sysex_data(SX.SET_PAN, [slot] + list(split_u14(value)))


def cmd_set_send(slot: int, send_index: int, value: int) -> List[int]:
    return sysex_data(SX.SET_SEND, [slot, send_index] + list(split_u14(value)))


def cmd_set_toggle(cmd: int, slot: int, on: bool) -> List[int]:
    return sysex_data(cmd, [slot, 1 if on else 0])


def cmd_set_qc(index: int, value: int) -> List[int]:
    return sysex_data(SX.SET_QC, [index] + list(split_u14(value)))


def cmd_locate(position: str) -> List[int]:
    return sysex_data(SX.LOCATE, encode_string(position))


def cmd_set_tempo(bpm_times_10: int) -> List[int]:
    return sysex_data(SX.SET_TEMPO, list(split_u14(bpm_times_10)))


def cmd_da_walk(limit: int) -> List[int]:
    return sysex_data(SX.DA_WALK, list(split_u14(limit)))


def cmd_da_set(seq: int, tag: int, value: int) -> List[int]:
    return sysex_data(SX.DA_SET, list(split_u14(seq)) + list(split_u21(tag)) + list(split_u14(value)))


def cmd_da_get(seq: int, tag: int) -> List[int]:
    return sysex_data(SX.DA_GET, list(split_u14(seq)) + list(split_u21(tag)))


def cmd_bank(step: int) -> List[int]:
    return sysex_data(SX.BANK, [step])


# ---------------------------------------------------------------------------
# Feedback parser: raw sysex data (after stripping F0/F7) -> event dict
# ---------------------------------------------------------------------------

def parse_feedback(data: List[int]) -> Optional[dict]:
    """Parse a 0x7D feedback SysEx from the script. Returns None if not ours."""
    if len(data) < 2 or data[0] != SYSEX_VENDOR_ID:
        return None
    cmd = data[1]
    body = data[2:]

    if cmd == SX.IDENT:
        text, _ = decode_string(body, 0)
        return {"type": "ident", "text": text}

    if cmd == SX.TRANSPORT and len(body) >= 3:
        flags = body[0]
        tempo10 = join_u14(body[1], body[2])
        idx = 3
        _, locator, idx = decode_flag_string(body, idx)
        _, fmt, idx = decode_flag_string(body, idx)
        return {
            "type": "transport",
            "play": bool(flags & 0x01),
            "record": bool(flags & 0x02),
            "cycle": bool(flags & 0x04),
            "metronome": bool(flags & 0x08),
            "tempo_bpm": tempo10 / 10.0,
            "locator": locator,
            "locator_format": fmt,
        }

    if cmd == SX.SLOT and len(body) >= 7:
        slot = body[0]
        flags = body[1]
        vol = join_u14(body[2], body[3])
        pan = join_u14(body[4], body[5])
        _, name, _ = decode_flag_string(body, 6)
        return {
            "type": "slot",
            "slot": slot,
            "exists": bool(flags & 0x20),
            "selected": bool(flags & 0x01),
            "mute": bool(flags & 0x02),
            "solo": bool(flags & 0x04),
            "record_arm": bool(flags & 0x08),
            "monitor": bool(flags & 0x10),
            "volume": vol / 16383.0,
            "pan": pan / 16383.0,
            "name": name,
        }

    if cmd == SX.QC and len(body) >= 3:
        return {
            "type": "qc",
            "index": body[0],
            "value": join_u14(body[1], body[2]) / 16383.0,
        }

    if cmd == SX.PLUGIN and len(body) >= 1:
        role = body[0]
        idx = 1
        name, idx = decode_string(body, idx)
        vendor, idx = decode_string(body, idx)
        version, idx = decode_string(body, idx)
        return {"type": "plugin", "role": role, "name": name, "vendor": vendor, "version": version}

    if cmd == SX.DA_PARAM_CHANGE and len(body) >= 7:
        return {
            "type": "da_param_change",
            "seq": join_u14(body[0], body[1]),
            "tag": join_u21(body[2], body[3], body[4]),
            "value": join_u14(body[5], body[6]) / 16383.0,
        }

    if cmd == SX.DA_OBJ and len(body) >= 5:
        walk_id = body[0]
        seq = join_u14(body[1], body[2])
        parent = join_u14(body[3], body[4])
        _, type_name, idx = decode_flag_string(body, 5)
        _, title, idx = decode_flag_string(body, idx)
        return {
            "type": "da_obj",
            "walk_id": walk_id,
            "seq": seq,
            "parent": parent,
            "object_type": type_name,
            "title": title,
        }

    if cmd == SX.DA_PARAM and len(body) >= 8:
        walk_id = body[0]
        seq = join_u14(body[1], body[2])
        tag = join_u21(body[3], body[4], body[5])
        value = join_u14(body[6], body[7]) / 16383.0
        _, title, idx = decode_flag_string(body, 8)
        _, display, idx = decode_flag_string(body, idx)
        return {
            "type": "da_param",
            "walk_id": walk_id,
            "seq": seq,
            "tag": tag,
            "value": value,
            "title": title,
            "display": display,
        }

    if cmd == SX.DA_DONE and len(body) >= 3:
        return {
            "type": "da_done",
            "walk_id": body[0],
            "count": join_u14(body[1], body[2]),
        }

    return {"type": "unknown", "cmd": cmd, "raw": list(body)}
