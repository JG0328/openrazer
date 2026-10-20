# SPDX-License-Identifier: GPL-2.0-or-later

import errno
import logging
import queue
import socket
import threading
import time
import unittest
from unittest import mock

from openrazer_daemon.misc.hidraw_transport import HidrawTransport

THREAD_NAME = 'hidraw-reader'


def make_frame(first=0x02, fill=0x00):
    return bytes((first,)) + bytes((fill,)) * 63


def validator(frame):
    """Refuse everything that does not start with report ID 2."""
    if frame[:1] != b'\x02':
        return 'not report 2'
    return None


def reader_threads():
    return [t for t in threading.enumerate() if t.name == THREAD_NAME]


class HidrawTransportTestCase(unittest.TestCase):
    def setUp(self):
        self.reports = queue.Queue()
        self.disconnected = threading.Event()
        self.disconnect_calls = 0
        self.logger = logging.getLogger('test_hidraw_transport')
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False
        # Cleanups run last-in first-out, so this one runs after every transport was closed
        self.addCleanup(self.assert_no_reader_threads)
        self.transport, self.dev = self.open_pair(self.reports.put)

    def assert_no_reader_threads(self):
        for thread in reader_threads():
            thread.join(1.0)
        self.assertEqual(reader_threads(), [])

    def open_pair(self, on_report):
        """Start a transport on one end of a SEQPACKET socketpair, return it with the device end."""
        host, dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        dev.settimeout(2.0)
        transport = HidrawTransport(host.detach(), on_report, self._on_disconnect, validator, self.logger)
        self.addCleanup(dev.close)
        self.addCleanup(transport.close)
        transport.start()
        return transport, dev

    def _on_disconnect(self):
        self.disconnect_calls += 1
        self.disconnected.set()

    def test_write_sends_exact_frame(self):
        frame = make_frame(fill=0x5A)
        self.assertTrue(self.transport.write(frame))
        self.assertEqual(self.dev.recv(4096), frame)

    def test_validator_refusal_returns_false_and_sends_nothing(self):
        self.dev.setblocking(False)
        with self.assertLogs(self.logger, level='WARNING') as logs:
            self.assertFalse(self.transport.write(make_frame(first=0x06)))
        self.assertIn('not report 2', logs.output[0])
        with self.assertRaises(BlockingIOError):
            self.dev.recv(4096)

    def test_request_returns_matching_reply(self):
        request_frame = make_frame(fill=0x11)
        unrelated = make_frame(fill=0x01)
        reply = make_frame(fill=0x02)

        def dongle():
            self.assertEqual(self.dev.recv(4096), request_frame)
            self.dev.send(unrelated)
            self.dev.send(reply)

        thread = threading.Thread(target=dongle)
        thread.start()
        result = self.transport.request(request_frame, lambda report: report[1] == 0x02, timeout=2.0)
        thread.join(2.0)

        self.assertEqual(result, reply)
        self.assertEqual(self.reports.get(timeout=1.0), unrelated)
        self.assertTrue(self.reports.empty())

    def test_request_timeout_returns_none(self):
        started = time.monotonic()
        result = self.transport.request(make_frame(), lambda report: True, timeout=0.1)
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 0.5)

    def test_unsolicited_report_goes_to_on_report(self):
        report = make_frame(fill=0x33)
        self.dev.send(report)
        self.assertEqual(self.reports.get(timeout=1.0), report)

    def test_peer_close_calls_on_disconnect_once(self):
        self.dev.close()

        self.assertTrue(self.disconnected.wait(1.0))
        self.assertEqual(self.disconnect_calls, 1)
        self.assertFalse(self.transport.alive)
        self.assertFalse(self.transport.write(make_frame()))
        self.transport.close()
        self.assertEqual(self.disconnect_calls, 1)

    def test_read_eio_is_disconnect(self):
        with mock.patch('openrazer_daemon.misc.hidraw_transport.os.read', side_effect=OSError(errno.EIO, 'EIO')):
            self.dev.send(make_frame())
            self.assertTrue(self.disconnected.wait(1.0))

        self.assertEqual(self.disconnect_calls, 1)
        self.assertFalse(self.transport.alive)
        self.assertTrue(self.reports.empty())

    def test_write_gap_is_respected(self):
        first = make_frame(fill=0x01)
        second = make_frame(fill=0x02)

        started = time.monotonic()
        self.assertTrue(self.transport.write(first))
        self.assertTrue(self.transport.write(second))
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.05)
        self.assertEqual(self.dev.recv(4096), first)
        self.assertEqual(self.dev.recv(4096), second)

    def test_close_is_idempotent(self):
        self.assertEqual(len(reader_threads()), 1)
        thread = reader_threads()[0]

        self.transport.close()
        self.transport.close()

        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.transport.alive)
        # The transport closed its end of the socket, so the device side sees EOF
        self.assertEqual(self.dev.recv(4096), b'')

    def test_closed_transport_refuses_write_and_request(self):
        self.transport.close()
        self.assertFalse(self.transport.write(make_frame()))
        self.assertIsNone(self.transport.request(make_frame(), lambda report: True, timeout=1.0))

    def test_request_returns_none_on_disconnect(self):
        results = []

        def requester():
            started = time.monotonic()
            results.append((self.transport.request(make_frame(), lambda report: True, timeout=5.0), time.monotonic() - started))

        thread = threading.Thread(target=requester)
        thread.start()
        # Once the frame has been written the waiter is certainly registered
        self.dev.recv(4096)
        self.dev.close()
        thread.join(2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        result, elapsed = results[0]
        self.assertIsNone(result)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(self.disconnect_calls, 1)

    def test_waiters_are_matched_in_registration_order(self):
        results = {}

        def requester(name):
            results[name] = self.transport.request(make_frame(fill=ord(name)), lambda report: True, timeout=2.0)

        threads = []
        for name in 'AB':
            thread = threading.Thread(target=requester, args=(name,))
            thread.start()
            threads.append(thread)
            # The frame is written after the waiter is registered, so waiter A is certainly first
            self.assertEqual(self.dev.recv(4096)[1], ord(name))

        self.dev.send(make_frame(fill=0x01))
        self.dev.send(make_frame(fill=0x02))
        for thread in threads:
            thread.join(2.0)

        self.assertEqual(results['A'], make_frame(fill=0x01))
        self.assertEqual(results['B'], make_frame(fill=0x02))
        self.assertTrue(self.reports.empty())

    def test_callback_exception_is_logged_and_does_not_kill_reader(self):
        calls = []
        got_both = threading.Event()

        def on_report(report):
            calls.append(report)
            if len(calls) == 2:
                got_both.set()
            if len(calls) == 1:
                raise RuntimeError('boom')

        transport, dev = self.open_pair(on_report)
        with self.assertLogs(self.logger, level='ERROR') as logs:
            dev.send(make_frame(fill=0x01))
            dev.send(make_frame(fill=0x02))
            self.assertTrue(got_both.wait(2.0))
        self.assertIn('boom', '\n'.join(logs.output))
        self.assertEqual(calls, [make_frame(fill=0x01), make_frame(fill=0x02)])
        self.assertTrue(transport.alive)

    def test_close_from_inside_callback_does_not_deadlock(self):
        closed = threading.Event()
        holder = []

        def on_report(report):
            holder[0].close()
            closed.set()

        transport, dev = self.open_pair(on_report)
        holder.append(transport)
        dev.send(make_frame())

        self.assertTrue(closed.wait(1.0))
        self.assertFalse(transport.alive)
        # The reader thread closes the fd itself once the callback returns
        self.assertEqual(dev.recv(4096), b'')


if __name__ == '__main__':
    unittest.main()
