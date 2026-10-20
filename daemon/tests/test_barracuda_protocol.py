# SPDX-License-Identifier: GPL-2.0-or-later

import unittest

from openrazer_daemon.misc.barracuda_protocol import (
    FRAME_LEN, LED_COUNT, PROP_BATTERY, PROP_BRIGHTNESS, PROP_CHARGING, PROP_IDLE_MINUTES, PROP_LINK, REPORT_ID,
    STATUS_EVENT, STATUS_OK, Report, build_color, build_get, build_set, check_frame, parse_report, reply_matcher)

GET_PROPS = (0x20, 0x21, 0x2A, 0x2C, 0x41)
SET_PROPS = (0x41, 0x2C)


def frame(prefix_hex, crc):
    """64 zero bytes with the given prefix at the start and crc at index 62."""
    buf = bytearray(64)
    prefix = bytes.fromhex(prefix_hex)
    buf[:len(prefix)] = prefix
    buf[62] = crc
    return bytes(buf)


def seal(buf, start=0):
    """Return buf as bytes with the CRC (XOR of bytes start..61) recomputed.

    Host frames use start=0, device reports use start=1.
    """
    buf = bytearray(buf)
    crc = 0
    for byte in buf[start:62]:
        crc ^= byte
    buf[62] = crc
    return bytes(buf)


def with_bytes(data, changes, start=0):
    """Copy of data with {index: value} applied and the CRC recomputed."""
    buf = bytearray(data)
    for index, value in changes.items():
        buf[index] = value
    return seal(buf, start)


REPLY_21 = frame("020260000000050080802101014f", 0x09)
EVENT_20 = frame("020a600000000500808020020101", 0x4D)
EVENT_21 = frame("020a60520000050080802102015e", 0x41)


class BarracudaProtocolTest(unittest.TestCase):

    def test_build_get_vectors(self):
        self.assertEqual(build_get(0x21), frame("0200600000000400008021", 0xC7))
        for prop, crc in ((0x20, 0xC6), (0x2A, 0xCC), (0x2C, 0xCA), (0x41, 0xA7)):
            with self.subTest(prop=prop):
                self.assertEqual(build_get(prop)[62], crc)
                self.assertEqual(len(build_get(prop)), FRAME_LEN)
        for bad in (-1, 256):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, build_get, bad)

    def test_build_set_vectors(self):
        self.assertEqual(build_set(0x41, 0xFF), frame("02006000000005000080c10001ff", 0xD8))
        for prop, value, crc in ((0x41, 0x40, 0x67), (0x41, 0x80, 0xA7), (0x2C, 7, 0x4D), (0x2C, 0, 0x4A), (0x2C, 60, 0x76)):
            with self.subTest(prop=prop, value=value):
                self.assertEqual(build_set(prop, value)[62], crc)
        self.assertEqual(build_set(0x2C, 7)[10:14], bytes.fromhex("ac000107"))
        for prop, value in ((-1, 0), (256, 0), (0x41, -1), (0x41, 256)):
            with self.subTest(prop=prop, value=value):
                self.assertRaises(ValueError, build_set, prop, value)

    def test_build_color_layout(self):
        f = build_color(255, 0, 0)
        self.assertEqual(f[:15], bytes.fromhex("020060000000170f03800000000005"))
        self.assertEqual(f[15:33], bytes([255, 0, 0]) * 6)
        self.assertEqual(f[33:62], bytes(29))
        self.assertEqual(f[62], 0xFC)
        self.assertEqual(f[63], 0)
        self.assertEqual(len(f), FRAME_LEN)
        self.assertEqual(LED_COUNT, 6)
        for args in ((-1, 0, 0), (0, 256, 0), (0, 0, -5)):
            with self.subTest(args=args):
                self.assertRaises(ValueError, build_color, *args)

    def test_check_frame_accepts_builders(self):
        accepted = [build_get(prop) for prop in GET_PROPS]
        accepted += [build_set(prop, value) for prop in SET_PROPS for value in (0, 1, 60, 255)]
        accepted += [build_color(0, 0, 0), build_color(255, 0, 0), build_color(1, 2, 3)]
        # Per-LED colours (six different RGB triplets) are a valid colour frame too.
        pattern = build_color(0, 0, 0)
        pattern = with_bytes(pattern, {15 + i: (i * 13 + 1) % 256 for i in range(18)})
        accepted.append(pattern)
        for data in accepted:
            with self.subTest(data=data.hex()):
                self.assertIsNone(check_frame(data))
        # Exhaustive sweep: only the whitelisted GET / SET ids are ever writable.
        for prop in range(256):
            with self.subTest(prop=prop):
                self.assertEqual(check_frame(build_get(prop)) is None, prop in GET_PROPS)
        # build_set takes the GET id and sets bit 7 itself, so sweep the GET id space.
        for prop in range(128):
            with self.subTest(prop=prop):
                self.assertEqual(check_frame(build_set(prop, 1)) is None, prop in SET_PROPS)
        # The constants are what the builders and the protocol notes use.
        self.assertEqual((REPORT_ID, FRAME_LEN), (0x02, 64))
        self.assertEqual((PROP_LINK, PROP_BATTERY, PROP_CHARGING, PROP_IDLE_MINUTES, PROP_BRIGHTNESS), GET_PROPS)

    def test_check_frame_rejects(self):
        get = build_get(0x21)
        set_ = build_set(0x41, 0x80)
        color = build_color(255, 0, 0)
        cases = (
            ("report id 6", with_bytes(get, {0: 6}), "report id"),
            ("length 63", get[:63], "length"),
            ("length 65", get + b"\x00", "length"),
            ("not bytes", "not bytes", "bytes"),
            ("None", None, "bytes"),
            ("GET 0x40", build_get(0x40), "not allowed"),
            ("SET 0xC0", build_set(0x40, 4), "not allowed"),
            ("SET 0x99", build_set(0x19, 1), "not allowed"),
            ("wrong CRC", get[:62] + bytes([get[62] ^ 0x01]) + get[63:], "crc"),
            ("byte 1 set", with_bytes(get, {1: 0x02}), "byte 1"),
            ("byte 2 not 0x60", with_bytes(get, {2: 0x00}), "byte 2"),
            ("byte 4 set", with_bytes(get, {4: 0x01}), "byte 4"),
            ("byte 9 = 0x00", with_bytes(get, {9: 0x00}), "byte 9"),
            ("class 0F/02", with_bytes(color, {8: 0x02}), "class/id"),
            ("class 0F/00", with_bytes(get, {7: 0x0F}), "class/id"),
            ("GET stray byte 20", with_bytes(get, {20: 0x01}), "byte 20"),
            ("GET wrong size", with_bytes(get, {6: 0x05}), "byte 6"),
            ("GET arg 11 set", with_bytes(get, {11: 0x01}), "byte 11"),
            ("SET N = 2", with_bytes(set_, {12: 0x02}), "byte 12"),
            ("SET N = 0", with_bytes(set_, {12: 0x00}), "byte 12"),
            ("SET wrong size", with_bytes(set_, {6: 0x04}), "byte 6"),
            ("SET stray byte 14", with_bytes(set_, {14: 0x01}), "byte 14"),
            ("color stray byte 33", with_bytes(color, {33: 0x01}), "byte 33"),
            ("color stray byte 61", with_bytes(color, {61: 0x01}), "byte 61"),
            ("color wrong size", with_bytes(color, {6: 0x14}), "byte 6"),
            ("color wrong start", with_bytes(color, {14: 0x04}), "byte 14"),
            ("byte 63 = 1", get[:63] + b"\x01", "byte 63"),
        )
        for name, data, expected in cases:
            with self.subTest(name):
                reason = check_frame(data)
                self.assertIsInstance(reason, str)
                self.assertIn(expected, reason)

    def test_parse_reply_and_event(self):
        self.assertEqual(parse_report(REPLY_21), Report('reply', 0x21, 0x02, 0x4F))
        self.assertEqual(parse_report(EVENT_20), Report('event', 0x20, 0x0A, 0x01))
        self.assertEqual(parse_report(EVENT_21), Report('event', 0x21, 0x0A, 0x5E))
        self.assertEqual((STATUS_OK, STATUS_EVENT), (0x02, 0x0A))
        # Any status other than 0x0A is a reply, and bytearray input is accepted.
        other = with_bytes(REPLY_21, {1: 0x03}, start=1)
        self.assertEqual(parse_report(bytearray(other)), Report('reply', 0x21, 0x03, 0x4F))

    def test_parse_rejects(self):
        def reply_with(**changes):
            return with_bytes(REPLY_21, {int(key[1:]): value for key, value in changes.items()}, start=1)

        cases = (
            ("bad CRC", REPLY_21[:62] + b"\x08" + REPLY_21[63:]),
            ("button report", b"\x0c\x01"),
            ("63 bytes", REPLY_21[:63]),
            ("65 bytes", REPLY_21 + b"\x00"),
            ("empty", b""),
            ("None", None),
            ("report id 6", reply_with(i0=0x06)),
            ("byte 2 not 0x60", reply_with(i2=0x00)),
            ("byte 8 = 0x00", reply_with(i8=0x00)),
            ("byte 9 = 0x00", reply_with(i9=0x00)),
        )
        for name, data in cases:
            with self.subTest(name):
                self.assertIsNone(parse_report(data))
        # Sanity: the helper really yields a valid reply when the change is harmless.
        self.assertEqual(reply_with(i3=0x00), REPLY_21)
        self.assertIsNotNone(parse_report(reply_with(i3=0x07)))

    def test_reply_matcher(self):
        match_21 = reply_matcher(0x21)
        self.assertIs(match_21(REPLY_21), True)
        self.assertIs(match_21(EVENT_21), False)
        self.assertIs(match_21(EVENT_20), False)
        self.assertIs(match_21(b"\x0c\x01"), False)
        self.assertIs(match_21(build_get(0x21)), False)
        self.assertIs(reply_matcher(0x20)(REPLY_21), False)
        self.assertRaises(ValueError, reply_matcher, 256)


if __name__ == '__main__':
    unittest.main()
