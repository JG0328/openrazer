# SPDX-License-Identifier: GPL-2.0-or-later

"""
Generic transport for a hidraw device node.

HidrawTransport owns a file descriptor, runs a single reader thread on it and offers a
small request/response layer on top: write() sends one frame (after a validator injected
by the caller has approved it), request() writes a frame and waits for the report that a
matcher accepts, everything else is handed to an on_report callback.

The transport knows nothing about the protocol spoken over it. It only uses os.read,
os.write and select on the descriptor, so any fd with hidraw semantics (one read returns
one whole report) works, e.g. a connected SOCK_SEQPACKET socket in tests.

Threading:
    * The reader thread is the only reader of the fd.
    * on_report and on_disconnect (and the matchers) run on the reader thread. Exceptions
      raised by them are logged and do not stop the thread.
    * write() and request() may be called from any thread except that they must not be
      called from a matcher. Writes are serialized and spaced by write_gap seconds.
    * close() may be called from any thread, including from a callback running on the
      reader thread (it does not join in that case; the reader closes the fd on its way out).
"""
import errno
import logging
import os
import select
import threading
import time
from typing import Callable, List, Optional

READ_SIZE = 1024
SELECT_TIMEOUT = 0.2
JOIN_TIMEOUT = 1.0

# A read or select failing with one of these means the device is gone
DISCONNECT_ERRNOS = frozenset((errno.EIO, errno.ENODEV, errno.EPIPE, errno.ECONNRESET, errno.EBADF))


def open_hidraw(path: str) -> int:
    """
    Open a hidraw node for reading and writing.

    :param path: Path of the node, e.g. /dev/hidraw3
    :return: The file descriptor (non-blocking, close-on-exec), owned by the caller
    :raises OSError: If the node cannot be opened
    """
    return os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)


class _Waiter(object):
    """A pending request() waiting for the report its matcher accepts"""
    __slots__ = ('matcher', 'event', 'report')

    def __init__(self, matcher: Callable[[bytes], bool]):
        self.matcher = matcher
        self.event = threading.Event()
        self.report = None  # type: Optional[bytes]


class HidrawTransport(object):
    """
    Reader thread plus request/response matching over a hidraw file descriptor
    """

    def __init__(self, fd: int, on_report: Callable[[bytes], None], on_disconnect: Callable[[], None],
                 validator: Callable[[bytes], Optional[str]], logger: logging.Logger, write_gap: float = 0.05):
        """
        :param fd: Open file descriptor, the transport takes ownership and closes it
        :param on_report: Called on the reader thread with every report no waiter wanted
        :param on_disconnect: Called once on the reader thread when the device goes away
        :param validator: Called with every frame before it is written. Returns None to
                          accept the frame or a string with the reason to refuse it
        :param logger: Logger for diagnostics
        :param write_gap: Minimum time in seconds between two writes
        """
        self._fd = fd
        self._on_report = on_report
        self._on_disconnect = on_disconnect
        self._validator = validator
        self._logger = logger
        self._write_gap = write_gap

        self._lock = threading.Lock()  # protects the state below
        self._alive = True
        self._closed = False
        self._thread = None  # type: Optional[threading.Thread]
        self._waiters = []  # type: List[_Waiter]

        self._write_lock = threading.Lock()  # serializes writes, protects _fd and _last_write
        self._last_write = None  # type: Optional[float]
        self._stop = threading.Event()

    @property
    def alive(self) -> bool:
        """False once the device disconnected or the transport was closed"""
        return self._alive

    def start(self) -> None:
        """Start the reader thread. Does nothing if already started or closed"""
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name='hidraw-reader', daemon=True)
            self._thread.start()

    def write(self, frame: bytes) -> bool:
        """
        Write one frame to the device.

        :param frame: The frame, it is checked by the validator before being written
        :return: True if the whole frame was written, False if the validator refused it,
                 the transport is closed or dead, or the write failed
        """
        frame = bytes(frame)  # the bytes that were validated are the bytes that get written
        if not self._alive:
            return False

        try:
            refusal = self._validator(frame)
        except Exception:
            self._logger.exception("Frame validator failed, refusing frame")
            return False
        if refusal is not None:
            self._logger.warning("Refusing to write frame: %s", refusal)
            return False

        with self._write_lock:
            if not self._alive or self._fd < 0:
                return False

            if self._last_write is not None:
                deadline = self._last_write + self._write_gap
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if self._stop.wait(remaining):
                        return False

            try:
                written = os.write(self._fd, frame)
            except OSError as exc:
                self._logger.warning("Write to hidraw failed: %s", exc)
                return False
            finally:
                self._last_write = time.monotonic()

            if written != len(frame):
                self._logger.warning("Short write to hidraw: %d of %d bytes", written, len(frame))
                return False
            return True

    def request(self, frame: bytes, matcher: Callable[[bytes], bool], timeout: float = 1.5) -> Optional[bytes]:
        """
        Write a frame and wait for the report that matcher accepts.

        The waiter is registered before the frame is written, so a fast reply is not lost.
        Waiters are offered each report in registration order.

        :param frame: The frame to write, see write()
        :param matcher: Called on the reader thread with each report, returns True to accept it
        :param timeout: Seconds to wait for the reply
        :return: The matching report, or None if the write failed, the timeout expired
                 or the device disconnected
        """
        waiter = _Waiter(matcher)
        with self._lock:
            if not self._alive:
                return None
            self._waiters.append(waiter)

        if not self.write(frame):
            self._remove_waiter(waiter)
            return None

        waiter.event.wait(timeout)
        self._remove_waiter(waiter)
        return waiter.report

    def close(self) -> None:
        """
        Stop the reader thread and close the fd. Safe to call more than once and from any
        thread. Does not call on_disconnect.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._alive = False
            thread = self._thread

        self._stop.set()
        self._wake_waiters()

        if thread is None:
            self._release_fd()
        elif thread is not threading.current_thread():
            thread.join(JOIN_TIMEOUT)
        # else: called from a callback, the reader closes the fd when the callback returns

    def _remove_waiter(self, waiter: _Waiter) -> None:
        with self._lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _wake_waiters(self) -> None:
        """Release every pending request() with None"""
        with self._lock:
            waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter.event.set()

    def _release_fd(self) -> None:
        with self._write_lock:
            fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError as exc:
                self._logger.debug("Closing hidraw fd failed: %s", exc)

    def _call(self, callback: Callable, *args) -> None:
        try:
            callback(*args)
        except Exception:
            self._logger.exception("Exception in hidraw callback")

    def _run(self) -> None:
        """Reader thread"""
        fd = self._fd
        try:
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select([fd], [], [], SELECT_TIMEOUT)
                except (OSError, ValueError) as exc:
                    if not self._stop.is_set():
                        self._handle_disconnect("select failed: {0}".format(exc))
                    break
                if not readable:
                    continue

                try:
                    data = os.read(fd, READ_SIZE)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as exc:
                    if not self._stop.is_set():
                        self._handle_disconnect("read failed: {0}".format(exc))
                    break

                if not data:
                    self._handle_disconnect("end of file")
                    break
                self._dispatch(data)
        except Exception:
            self._logger.exception("Unexpected error in hidraw reader, treating as disconnect")
            self._handle_disconnect("reader error")
        finally:
            with self._lock:
                self._alive = False
            self._wake_waiters()
            self._release_fd()

    def _handle_disconnect(self, reason: str) -> None:
        """The device is gone (reader thread): mark dead, release waiters, notify once"""
        with self._lock:
            first = self._alive
            self._alive = False
        self._wake_waiters()
        if first:
            self._logger.info("hidraw device disconnected (%s)", reason)
            self._call(self._on_disconnect)

    def _dispatch(self, report: bytes) -> None:
        """Give a report to the first waiter that accepts it, otherwise to on_report"""
        with self._lock:
            waiters = list(self._waiters)

        for waiter in waiters:
            try:
                accepted = waiter.matcher(report)
            except Exception:
                self._logger.exception("Exception in hidraw matcher")
                continue
            if not accepted:
                continue
            with self._lock:
                # It may have timed out in the meantime, then try the next one
                if waiter not in self._waiters:
                    continue
                self._waiters.remove(waiter)
                waiter.report = report
            waiter.event.set()
            return

        self._call(self._on_report, report)
