# SPDX-License-Identifier: GPL-2.0-or-later

"""
Fake 2.4 GHz dongle of the Razer Barracuda X Chroma, for tests (no test cases in here).

FakeDongle plays the device end of a SOCK_SEQPACKET socket: one recv is one frame, like a
hidraw node. It answers GET and SET frames the way the real dongle does (spec section 3),
sends events on demand and records every frame it receives. Every received frame is also
run through barracuda_protocol.check_frame; the ones the gate refuses go to `rejected`
and are not answered.

In test mode the device class connects to an abstract socket whose name it reads from
the fake_hidraw_socket file of its sysfs directory, see make_wireless().

Standalone, for the end-to-end smoke test:
    python3 daemon/tests/fake_hidraw.py --abstract NAME [--log FILE]
serves NAME until it is killed and appends one hex line per received frame to FILE.
"""

import argparse
import itertools
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
from typing import Optional, TextIO

if __name__ == '__main__':
    # Run as a script: use the daemon package of this checkout, not an installed one
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from openrazer_daemon.misc import barracuda_protocol

POLL_TIMEOUT = 0.1
JOIN_TIMEOUT = 2.0

_socket_names = itertools.count()


def _device_report(status: int, seq: int, prop: int, kind: int, value: int) -> bytes:
    """Report as the dongle sends it, with the CRC over bytes 1..61"""
    buf = bytearray(barracuda_protocol.FRAME_LEN)
    buf[0] = barracuda_protocol.REPORT_ID
    buf[1] = status
    buf[2] = 0x60
    buf[3] = seq
    buf[6] = 0x05
    buf[8] = 0x80
    buf[9] = 0x80
    buf[10] = prop
    buf[11] = kind
    buf[12] = 0x01
    buf[13] = value
    crc = 0
    for byte in buf[1:62]:
        crc ^= byte
    buf[62] = crc
    return bytes(buf)


class FakeDongle(object):
    """
    Device end of the fake hidraw node.

    Attributes the tests read:
        props: property id (the GET id) -> value; SET frames update it
        silent_props: GET ids that are never answered, as if the reply got lost
        writes: every frame received, in order
        rejected: the received frames that check_frame refused
        log_file: if set to a text file, one hex line is written to it per received frame
    """
    DEFAULT_PROPS = {0x20: 1, 0x21: 79, 0x2A: 0, 0x2C: 5, 0x41: 84}

    def __init__(self, props: Optional[dict] = None, silent: bool = False, silent_props=()):
        self.props = dict(self.DEFAULT_PROPS if props is None else props)
        self.silent = silent
        self.silent_props = set(silent_props)
        self.writes = []  # type: list
        self.rejected = []  # type: list
        self.log_file = None  # type: Optional[TextIO]

        self._cond = threading.Condition()  # protects everything below and the lists above
        self._socks = []  # type: list
        self._threads = []  # type: list
        self._listener = None  # type: Optional[socket.socket]
        self._seq = 0
        self._stop = threading.Event()

    def attach(self, sock: socket.socket) -> None:
        """Serve one connected socket in a thread"""
        sock.settimeout(POLL_TIMEOUT)
        thread = threading.Thread(target=self._serve, args=(sock,), name='fake-dongle', daemon=True)
        with self._cond:
            if self._stop.is_set():
                sock.close()
                return
            self._socks.append(sock)
            self._threads.append(thread)
        thread.start()

    def listen_abstract(self, name: str) -> None:
        """
        Accept connections on the abstract SOCK_SEQPACKET socket name in a thread.

        The socket is bound and listening when this returns, so a connect() right after succeeds.
        """
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind('\0' + name)
        listener.listen(4)
        listener.settimeout(POLL_TIMEOUT)
        thread = threading.Thread(target=self._accept, args=(listener,), name='fake-dongle-listener', daemon=True)
        with self._cond:
            self._listener = listener
            self._threads.append(thread)
        thread.start()

    def emit_event(self, prop: int, value: int) -> None:
        """Send a spontaneous event to every connected end, like the headset does"""
        with self._cond:
            self.props[prop] = value
            event = _device_report(barracuda_protocol.STATUS_EVENT, self._seq, prop, 0x02, value)
            self._seq = (self._seq + 1) & 0xFF
            socks = list(self._socks)
        for sock in socks:
            try:
                sock.send(event)
            except OSError:
                pass

    def wait_for_writes(self, n: int, timeout: float = 2.0) -> bool:
        """Wait until at least n frames were received"""
        with self._cond:
            return self._cond.wait_for(lambda: len(self.writes) >= n, timeout)

    def close(self) -> None:
        """Hang up every connection (the device sees end of file) and stop the threads"""
        self._stop.set()
        with self._cond:
            socks, self._socks = self._socks, []
            threads, self._threads = self._threads, []
            listener, self._listener = self._listener, None

        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(JOIN_TIMEOUT)
        for sock in socks:
            sock.close()
        if listener is not None:
            listener.close()

    def _accept(self, listener: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.attach(conn)

    def _serve(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                frame = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not frame:
                break

            reply = self._receive(frame)
            if reply is not None:
                try:
                    sock.send(reply)
                except OSError:
                    break

    def _receive(self, frame: bytes) -> Optional[bytes]:
        """Record a frame, return the reply to send, if any"""
        refusal = barracuda_protocol.check_frame(frame)
        with self._cond:
            self.writes.append(frame)
            if refusal is not None:
                self.rejected.append(frame)
            if self.log_file is not None:
                self.log_file.write(frame.hex() + '\n')
                self.log_file.flush()
            self._cond.notify_all()

            if refusal is not None or self.silent or frame[7:9] != b'\x00\x00':
                # Refused, deaf, or a colour frame (the real dongle does not acknowledge those)
                return None

            prop = frame[10]
            if prop & 0x80:
                self.props[prop & 0x7F] = frame[13]
                value = 0x00
            elif prop in self.props and prop not in self.silent_props:
                value = self.props[prop]
            else:
                return None
        return _device_report(barracuda_protocol.STATUS_OK, 0x00, prop, 0x01, value)


def make_wireless(case, dongle: FakeDongle, config=None):
    """
    Build a RazerBarracudaXChromaWireless that talks to dongle, without D-Bus.

    The device, the dongle and the fake sysfs directory are cleaned up with case.addCleanup.
    config is the daemon configuration, hidraw_fixtures.make_config() if None.
    """
    # Imported here so that the standalone dongle does not need the daemon's D-Bus side
    from openrazer_daemon.hardware.hidraw_devices import RazerBarracudaXChromaWireless
    from hidraw_fixtures import build_device, make_sysfs_hid

    root = tempfile.mkdtemp(prefix='fake_hidraw_')
    case.addCleanup(shutil.rmtree, root, ignore_errors=True)
    dev_path = make_sysfs_hid(root, '0003:1532:0574.0001')

    name = 'openrazer-fake-hidraw-{0}-{1}'.format(os.getpid(), next(_socket_names))
    with open(os.path.join(dev_path, 'fake_hidraw_socket'), 'w') as name_file:
        name_file.write(name + '\n')

    dongle.listen_abstract(name)
    case.addCleanup(dongle.close)

    device = build_device(RazerBarracudaXChromaWireless, dev_path, config)
    case.addCleanup(device.close)
    return device


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Fake Razer Barracuda X Chroma dongle on an abstract SOCK_SEQPACKET socket')
    parser.add_argument('--abstract', required=True, metavar='NAME', help='abstract socket name to listen on')
    parser.add_argument('--log', metavar='FILE', help='append one hex line per received frame to FILE')
    args = parser.parse_args(argv)

    # Let a plain kill run the clean-up below
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))

    dongle = FakeDongle()
    log_file = None
    try:
        if args.log:
            log_file = open(args.log, 'a')
            dongle.log_file = log_file
        dongle.listen_abstract(args.abstract)
        while True:
            signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        dongle.close()
        if log_file is not None:
            log_file.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
