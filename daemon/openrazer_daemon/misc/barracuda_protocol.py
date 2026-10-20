# SPDX-License-Identifier: GPL-2.0-or-later

"""
Frame helpers for the Razer Barracuda X Chroma property protocol.

Everything travels as 64 byte HID report 2 (vendor page 0xFF14) on the hidraw node of
the dongle. This module is pure (no I/O): it builds host frames, parses device reports
and, most importantly, provides check_frame(), the gate every frame must pass before it
is written to the real device. Only a small whitelist of GET / SET / colour frames is
accepted, everything else (including anything that merely looks close) is refused.

Host frame layout:
    [0]=02 [1]=00 [2]=60 [3..5]=00 [6]=SZ [7]=class [8]=id [9]=80 [10..61]=args
    [62]=XOR(bytes 0..61) [63]=00
Device report layout (reply or event):
    [0]=02 [1]=status [2]=60 [6]=SZ [8]=80 [9]=80 [10]=prop [13]=value [62]=XOR(bytes 1..61)
"""
from typing import Callable, NamedTuple, Optional

REPORT_ID = 0x02
FRAME_LEN = 64
LED_COUNT = 6

PROP_LINK = 0x20
PROP_BATTERY = 0x21
PROP_CHARGING = 0x2A
PROP_IDLE_MINUTES = 0x2C
PROP_BRIGHTNESS = 0x41

STATUS_OK = 0x02
STATUS_EVENT = 0x0A

# Properties that check_frame lets through. SET ids are the GET id with bit 7 set.
_GET_PROPS = frozenset((PROP_LINK, PROP_BATTERY, PROP_CHARGING, PROP_IDLE_MINUTES, PROP_BRIGHTNESS))
_SET_PROPS = frozenset((PROP_BRIGHTNESS | 0x80, PROP_IDLE_MINUTES | 0x80))

_CLASS_PROPERTY = (0x00, 0x00)
_CLASS_COLOR = (0x0F, 0x03)

_COLOR_ARGS = bytes((0x00, 0x00, 0x00, 0x00, LED_COUNT - 1))  # row 0, LEDs 0..5
_COLOR_FIRST = 15
_COLOR_LAST = _COLOR_FIRST + 3 * LED_COUNT


class Report(NamedTuple):
    """A parsed device report: kind is 'reply' or 'event'."""
    kind: str
    prop: int
    status: int
    value: int


def _byte(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise ValueError('{0} must be an integer in 0..255, got {1!r}'.format(name, value))
    return value


def _xor(data, start: int, end: int) -> int:
    crc = 0
    for byte in data[start:end]:
        crc ^= byte
    return crc


def _frame(size: int, class_id: tuple, args: bytes) -> bytes:
    buf = bytearray(FRAME_LEN)
    buf[0] = REPORT_ID
    buf[2] = 0x60
    buf[6] = size
    buf[7], buf[8] = class_id
    buf[9] = 0x80
    buf[10:10 + len(args)] = args
    buf[62] = _xor(buf, 0, 62)
    return bytes(buf)


def _color_frame(rgb: bytes) -> bytes:
    return _frame(5 + 3 * LED_COUNT, _CLASS_COLOR, _COLOR_ARGS + rgb)


def build_get(prop: int) -> bytes:
    """Frame that reads property ``prop``."""
    return _frame(4, _CLASS_PROPERTY, bytes((_byte('prop', prop), 0x00, 0x00)))


def build_set(prop: int, value: int) -> bytes:
    """Frame that writes one byte ``value`` to the property whose GET id is ``prop``."""
    return _frame(5, _CLASS_PROPERTY, bytes((_byte('prop', prop) | 0x80, 0x00, 0x01, _byte('value', value))))


def build_color(red: int, green: int, blue: int) -> bytes:
    """Per-LED colour frame with all LED_COUNT LEDs set to the same colour."""
    return _color_frame(bytes((_byte('red', red), _byte('green', green), _byte('blue', blue))) * LED_COUNT)


def check_frame(frame: bytes) -> Optional[str]:
    """
    Safety gate for writes to the device.

    Returns None if the frame may be written, otherwise the reason it is refused.
    Accepted: GET of the known properties, SET of brightness / idle minutes with one
    data byte, and the per-LED colour frame. Every other byte must match exactly.
    """
    if not isinstance(frame, (bytes, bytearray)):
        return 'frame is not bytes'
    if len(frame) != FRAME_LEN:
        return 'frame length is {0}, expected {1}'.format(len(frame), FRAME_LEN)
    if frame[0] != REPORT_ID:
        return 'report id is 0x{0:02x}, expected 0x{1:02x}'.format(frame[0], REPORT_ID)
    if frame[62] != _xor(frame, 0, 62):
        return 'crc is 0x{0:02x}, expected 0x{1:02x}'.format(frame[62], _xor(frame, 0, 62))

    class_id = (frame[7], frame[8])
    if class_id == _CLASS_PROPERTY:
        prop = frame[10]
        if prop & 0x80:
            if prop not in _SET_PROPS:
                return 'SET of property 0x{0:02x} is not allowed'.format(prop)
            expected = build_set(prop & 0x7F, frame[13])
        else:
            if prop not in _GET_PROPS:
                return 'GET of property 0x{0:02x} is not allowed'.format(prop)
            expected = build_get(prop)
    elif class_id == _CLASS_COLOR:
        expected = _color_frame(bytes(frame[_COLOR_FIRST:_COLOR_LAST]))
    else:
        return 'class/id {0:02x}/{1:02x} is not allowed'.format(*class_id)

    # The expected frame fixes every byte except the data byte(s) it was built from,
    # so this covers the envelope, the sizes, the arguments and the zero padding.
    for index in range(FRAME_LEN):
        if frame[index] != expected[index]:
            return 'byte {0} is 0x{1:02x}, expected 0x{2:02x}'.format(index, frame[index], expected[index])
    return None


def parse_report(data: bytes) -> Optional[Report]:
    """
    Parse a device input report 2, or return None if it is not one.

    Anything that is not a well formed 64 byte report (wrong length, envelope or CRC,
    or other reports of the node such as the 2 byte button reports) gives None.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) != FRAME_LEN:
        return None
    if data[0] != REPORT_ID or data[2] != 0x60 or data[8] != 0x80 or data[9] != 0x80:
        return None
    if data[62] != _xor(data, 1, 62):
        return None
    kind = 'event' if data[1] == STATUS_EVENT else 'reply'
    return Report(kind, data[10], data[1], data[13])


def reply_matcher(prop: int) -> Callable[[bytes], bool]:
    """Matcher that accepts only the reply (not events) to property ``prop``."""
    _byte('prop', prop)

    def matches(data: bytes) -> bool:
        report = parse_report(data)
        return report is not None and report.kind == 'reply' and report.prop == prop

    return matches
