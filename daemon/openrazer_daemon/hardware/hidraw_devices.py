# SPDX-License-Identifier: GPL-2.0-or-later

"""
Devices that have no OpenRazer kernel driver

They sit on hid-generic, so there is no device_type or device_serial file in
their sysfs directory. They are found by the hidraw/ directory the kernel
creates for them, and the daemon talks to them through that node (or, for the
passive devices in this module, not at all).

The helper base class starts with an underscore so that get_device_classes()
does not register it.
"""

import os
import re
import socket
import threading
from typing import Optional

from openrazer_daemon.hardware.device_base import RazerDevice as _RazerDevice
from openrazer_daemon.misc import barracuda_protocol as _protocol
from openrazer_daemon.misc import hidraw_transport as _hidraw_transport


class _RazerHidrawDevice(_RazerDevice):
    """
    Base class for devices driven through hidraw instead of a kernel driver

    By default the device is passive: it is published on D-Bus but its hidraw
    node is never opened.
    """
    USB_VID = 0x1532
    HIDRAW_DEVICE = True
    # Subclasses that talk to the device set this to True
    OPENS_HIDRAW = False
    DEVICE_IMAGE = ''
    HIDRAW_DEVICE_NAME = ''

    @classmethod
    def match(cls, device_id, dev_path):
        """
        Match against the device ID and the lack of a kernel driver

        :param device_id: Device ID like 0003:1532:0000.0000
        :type device_id: str

        :param dev_path: Device path. Normally '/sys/bus/hid/devices/0003:1532:0000.0000'
        :type dev_path: str

        :return: True if its the correct device ID, has no device_type file and has a hidraw node
        :rtype: bool
        """
        try:
            if re.fullmatch(r'0003:%04X:%04X\.[0-9A-F]{4}' % (cls.USB_VID, cls.USB_PID), device_id) is None:
                return False

            if 'device_type' in os.listdir(dev_path):
                return False

            return len(os.listdir(os.path.join(dev_path, 'hidraw'))) > 0
        except OSError:
            return False

    @staticmethod
    def hidraw_node(dev_path: str) -> Optional[str]:
        """
        Get the path of the hidraw node of a HID device

        :param dev_path: Device path
        :type dev_path: str

        :return: Path in /dev, None if the device has no hidraw node
        :rtype: str or None
        """
        try:
            entries = sorted(os.listdir(os.path.join(dev_path, 'hidraw')))
        except OSError:
            return None

        if not entries:
            return None

        return '/dev/' + entries[0]

    @classmethod
    def check_access(cls, dev_path, testing):
        """
        Check that the daemon is allowed to use the device

        :param dev_path: Device path
        :type dev_path: str

        :param testing: True when the daemon runs against a test directory
        :type testing: bool

        :return: True if the hidraw node is not needed or can be read and written
        :rtype: bool
        """
        if testing or not cls.OPENS_HIDRAW:
            return True

        node = cls.hidraw_node(dev_path)

        return node is not None and os.access(node, os.R_OK | os.W_OK)

    def get_serial(self):
        """
        Get the serial number, made up from the USB IDs because the device has no readable one

        :return: String of the serial number
        :rtype: str
        """
        return "HIDRAW%04X%04X" % (self.USB_VID, self.USB_PID)

    def get_device_mode(self):
        return '0:0'

    def set_device_mode(self, mode_id, param):
        pass

    def restore_brightness(self):
        pass

    def disable_brightness(self):
        pass

    def _dbus_get_device_name(self):
        return self.HIDRAW_DEVICE_NAME

    def _dbus_get_firmware(self):
        return 'unknown'

    def _extra_dbus_methods(self):
        """
        Override to add D-Bus methods

        :return: List of (interface, method, callback, in-args, out-args) tuples
        :rtype: list
        """
        return []

    def load_methods(self):
        """
        Load DBus methods

        The stock getDeviceName and getFirmware read files only the kernel driver provides, so replace them.
        """
        super().load_methods()

        methods = [
            ('razer.device.misc', 'getDeviceName', self._dbus_get_device_name, None, 's'),
            ('razer.device.misc', 'getFirmware', self._dbus_get_firmware, None, 's'),
        ]
        methods.extend(self._extra_dbus_methods())

        for m in methods:
            self.logger.debug("Adding {}.{} method to DBus".format(m[0], m[1]))
            self.add_dbus_method(m[0], m[1], m[2], in_signature=m[3], out_signature=m[4])


class RazerBarracudaXChromaWired(_RazerHidrawDevice):
    """
    Class for the Razer Barracuda X Chroma (Wired)
    """
    USB_PID = 0x0573
    HIDRAW_DEVICE_NAME = 'Razer Barracuda X Chroma (Wired)'

    METHODS = ['get_device_type_headset']


class RazerSeirenV3Mini(_RazerHidrawDevice):
    """
    Class for the Razer Seiren V3 Mini
    """
    USB_PID = 0x056A
    HIDRAW_DEVICE_NAME = 'Razer Seiren V3 Mini'

    METHODS = ['get_device_type_accessory']


class RazerBarracudaXChromaWireless(_RazerHidrawDevice):
    """
    Class for the Razer Barracuda X Chroma (Wireless), driven through the hidraw node of its 2.4 GHz dongle

    The dongle stays enumerated while the headset is off or on Bluetooth, so the device
    is always published. The node is kept open (usbhid only polls the dongle while it
    is) and a reader thread caches what the dongle reports: battery, charging and link
    events. The D-Bus getters only read that cache, they never cause radio traffic.

    Every time the link comes up (and once at startup) a sync thread reads the state
    and applies again the lighting chosen in the daemon, because the headset does not
    keep its colour when it is turned off. Brightness and idle time are saved in the
    headset itself, so they are only written when they are changed; a change made
    while the headset is off is written at the next link up.

    Threading: _state_lock protects the cached state and self.zone; it is never held
    while writing or waiting for the device. _write_lock makes "decide what to write,
    then write it" one step for the D-Bus thread and the sync thread, so the frame
    written last always matches the latest state.
    """
    USB_PID = 0x0574
    HIDRAW_DEVICE_NAME = 'Razer Barracuda X Chroma (Wireless)'
    OPENS_HIDRAW = True

    METHODS = ['get_device_type_headset', 'set_static_effect', 'set_none_effect', 'get_brightness', 'set_brightness', 'get_battery', 'is_charging', 'get_idle_time', 'set_idle_time']

    SYNC_JOIN_TIMEOUT = 2.0

    def __init__(self, *args, **kwargs):
        # The base constructor may already call the D-Bus methods (the BatteryNotifier
        # takes getBattery, restore_effect calls setStatic), so the state comes first
        self._state_lock = threading.Lock()
        self._link = False
        self._battery = None  # type: Optional[int]
        self._charging = False
        self._idle_minutes = None  # type: Optional[int]
        self._pending_idle_minutes = None  # type: Optional[int]
        self._pending_brightness = None  # type: Optional[int]
        self._lights_suspended = False
        self._sync_thread = None  # type: Optional[threading.Thread]
        self._sync_again = False
        self._closing = False
        self._write_lock = threading.Lock()
        self._transport = None  # type: Optional[_hidraw_transport.HidrawTransport]

        super().__init__(*args, **kwargs)

        try:
            fd = self._open_fd()
        except OSError as exc:
            self.logger.warning("Cannot open the dongle of %s, battery and lighting are unavailable: %s", self.HIDRAW_DEVICE_NAME, exc)
            self._transport = None
            return

        self._transport = _hidraw_transport.HidrawTransport(fd, self._on_report, self._on_disconnect, _protocol.check_frame, self.logger)
        self._transport.start()
        self._request_sync()

    def _open_fd(self) -> int:
        """
        Open the hidraw node of the dongle

        In test mode there is no node: the fake dongle (daemon/tests/fake_hidraw.py) listens
        on the abstract SOCK_SEQPACKET socket named in <device_path>/fake_hidraw_socket.

        :return: File descriptor, owned by the caller
        :rtype: int

        :raises OSError: If it cannot be opened
        """
        if self._testing:
            with open(os.path.join(self._device_path, 'fake_hidraw_socket')) as name_file:
                name = name_file.read().strip()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            try:
                sock.connect('\0' + name)
                sock.setblocking(False)
            except OSError:
                sock.close()
                raise
            return sock.detach()

        node = self.hidraw_node(self._device_path)
        if node is None:
            raise FileNotFoundError("No hidraw node in {0}".format(self._device_path))
        return _hidraw_transport.open_hidraw(node)

    def _extra_dbus_methods(self):
        return [
            ('razer.device.power', 'getBattery', self._dbus_get_battery, None, 'd'),
            ('razer.device.power', 'isCharging', self._dbus_is_charging, None, 'b'),
            ('razer.device.power', 'getIdleTime', self._dbus_get_idle_time, None, 'q'),
            ('razer.device.power', 'setIdleTime', self._dbus_set_idle_time, 'q', None),
            ('razer.device.lighting.chroma', 'setStatic', self._dbus_set_static, 'yyy', None),
            ('razer.device.lighting.chroma', 'setNone', self._dbus_set_none, None, None),
            ('razer.device.lighting.brightness', 'setBrightness', self._dbus_set_brightness, 'd', None),
        ]

    def _linked(self) -> bool:
        """True if frames can reach the headset now. Call with _state_lock held"""
        return self._transport is not None and self._link

    def _dbus_get_battery(self) -> float:
        """
        Get the battery level from the cache

        :return: Last known level 0..100, -1.0 if it was never known
        :rtype: float
        """
        self.logger.debug("DBus call get_battery")

        with self._state_lock:
            battery = self._battery

        return -1.0 if battery is None else float(battery)

    def _dbus_is_charging(self) -> bool:
        """
        Get the charging state from the cache

        :return: True only if the headset is linked and said it is charging
        :rtype: bool
        """
        self.logger.debug("DBus call is_charging")

        with self._state_lock:
            return bool(self._charging and self._link)

    def _dbus_get_idle_time(self) -> int:
        """
        Get the idle time after which the headset turns itself off

        :return: Seconds, 0 if not known
        :rtype: int
        """
        self.logger.debug("DBus call get_idle_time")

        with self._state_lock:
            minutes = self._idle_minutes

        return 0 if minutes is None else minutes * 60

    def _dbus_set_idle_time(self, idle_time):
        """
        Set the idle time after which the headset turns itself off

        The headset counts in minutes. Without link the value is kept and written when
        the link comes up.

        :param idle_time: Seconds
        :type idle_time: int
        """
        self.logger.debug("DBus call set_idle_time")

        minutes = min(60, max(0, round(idle_time / 60)))

        with self._write_lock:
            with self._state_lock:
                linked = self._linked()
                if not linked:
                    self._pending_idle_minutes = minutes

            if linked:
                self._write_idle_minutes(minutes)

    def _write_idle_minutes(self, minutes: int) -> None:
        """Write the idle minutes, keep them pending if that fails. Call with _write_lock held"""
        if self._transport.write(_protocol.build_set(_protocol.PROP_IDLE_MINUTES, minutes)):
            with self._state_lock:
                self._idle_minutes = minutes
                self._pending_idle_minutes = None
        else:
            with self._state_lock:
                self._pending_idle_minutes = minutes

    def _dbus_set_static(self, red, green, blue):
        """
        Set all the LEDs to one colour

        :param red: Red component
        :type red: int

        :param green: Green component
        :type green: int

        :param blue: Blue component
        :type blue: int
        """
        self.logger.debug("DBus call set_static_effect")

        red, green, blue = int(red), int(green), int(blue)
        # Built first: a colour out of range raises before anything changed
        frame = _protocol.build_color(red, green, blue)

        # Notify others
        self.send_effect_event('setStatic', red, green, blue)

        with self._write_lock:
            with self._state_lock:
                # remember effect
                self.set_persistence('backlight', 'effect', 'static')
                self.zone['backlight']['colors'][0:3] = red, green, blue
                send = self._linked() and not self._lights_suspended

            if send:
                self._transport.write(frame)

    def _dbus_set_none(self):
        """
        Turn the LEDs off, with a black colour so that the brightness saved in the headset stays
        """
        self.logger.debug("DBus call set_none_effect")

        # Notify others
        self.send_effect_event('setNone')

        with self._write_lock:
            with self._state_lock:
                # remember effect
                self.set_persistence('backlight', 'effect', 'none')
                send = self._linked()

            if send:
                self._transport.write(_protocol.build_color(0, 0, 0))

    def _dbus_set_brightness(self, brightness):
        """
        Set the brightness, which the headset saves itself

        Brightness changes that effect sync brings from other devices are ignored, so
        that changing another device does not change what the headset saved. Without
        link the value is kept and written when the link comes up.

        :param brightness: Brightness 0..100
        :type brightness: float
        """
        self.logger.debug("DBus call set_brightness")

        if self.disable_notify:
            return

        brightness = float(min(100, max(0, brightness)))
        value = int(round(brightness * 255 / 100))

        with self._write_lock:
            with self._state_lock:
                self.set_persistence('backlight', 'brightness', brightness)
                linked = self._linked()
                if not linked:
                    self._pending_brightness = value

            if linked:
                self._write_brightness(value)

    def _write_brightness(self, value: int) -> None:
        """Write the brightness (0..255), keep it pending if that fails. Call with _write_lock held"""
        if self._transport.write(_protocol.build_set(_protocol.PROP_BRIGHTNESS, value)):
            with self._state_lock:
                self._pending_brightness = None
        else:
            with self._state_lock:
                self._pending_brightness = value

    def _on_report(self, data: bytes) -> None:
        """
        Handle a report nobody waited for (reader thread)

        :param data: The report
        :type data: bytes
        """
        report = _protocol.parse_report(data)
        if report is None or report.kind != 'event':
            return

        link_up = False
        with self._state_lock:
            if report.prop == _protocol.PROP_BATTERY:
                self._battery = min(report.value, 100)
            elif report.prop == _protocol.PROP_CHARGING:
                self._charging = bool(report.value)
            elif report.prop == _protocol.PROP_LINK:
                self._link = report.value == 1
                if self._link:
                    link_up = True
                else:
                    self._charging = False

        if report.prop == _protocol.PROP_LINK:
            self.logger.info("Headset link %s", 'up' if link_up else 'down')
        if link_up:
            self._request_sync()

    def _on_disconnect(self) -> None:
        """The dongle went away (reader thread)"""
        with self._state_lock:
            self._link = False
            self._charging = False

    def _request_sync(self) -> None:
        """Run _sync in its own thread, or once more after the one that is running"""
        with self._state_lock:
            if self._closing or self._transport is None:
                return
            if self._sync_thread is not None:
                self._sync_again = True
                return
            self._sync_thread = threading.Thread(target=self._sync_loop, name='barracuda-sync', daemon=True)
            self._sync_thread.start()

    def _sync_loop(self) -> None:
        while True:
            try:
                self._sync()
            except Exception:
                self.logger.exception("Failed to sync with the headset")

            with self._state_lock:
                if self._closing or not self._sync_again:
                    self._sync_thread = None
                    return
                self._sync_again = False

    def _get(self, prop: int) -> Optional[int]:
        """
        Read a property (sync thread only, never the reader thread)

        :return: The value, None if there was no good reply
        :rtype: int or None
        """
        reply = self._transport.request(_protocol.build_get(prop), _protocol.reply_matcher(prop))
        if reply is None:
            return None

        report = _protocol.parse_report(reply)
        if report is None or report.status != _protocol.STATUS_OK:
            return None

        return report.value

    def _sync(self) -> None:
        """Read the state of the headset into the cache and apply the lighting again (sync thread)"""
        if self._get(_protocol.PROP_LINK) != 1:
            with self._state_lock:
                self._link = False
            self.logger.debug("Headset not linked")
            return

        with self._state_lock:
            self._link = True

        for prop in (_protocol.PROP_BATTERY, _protocol.PROP_CHARGING, _protocol.PROP_IDLE_MINUTES, _protocol.PROP_BRIGHTNESS):
            value = self._get(prop)
            with self._state_lock:
                if value is None:
                    if not self._link:
                        # Gone again, the next link up syncs
                        return
                elif prop == _protocol.PROP_BATTERY:
                    self._battery = min(value, 100)
                elif prop == _protocol.PROP_CHARGING:
                    self._charging = bool(value)
                elif prop == _protocol.PROP_IDLE_MINUTES:
                    self._idle_minutes = value
                elif self._pending_brightness is None:
                    # The headset keeps its brightness, only show it. A pending change
                    # made while it was off wins, it is written below
                    self.zone['backlight']['brightness'] = round(value * 100 / 255, 1)

        with self._write_lock:
            with self._state_lock:
                pending = self._pending_idle_minutes if self._linked() else None
            if pending is not None:
                self._write_idle_minutes(pending)

            with self._state_lock:
                pending = self._pending_brightness if self._linked() else None
            if pending is not None:
                self._write_brightness(pending)

            with self._state_lock:
                frame = self._lighting_frame() if self._linked() else None
            if frame is not None:
                self._transport.write(frame)

    def _lighting_frame(self) -> Optional[bytes]:
        """
        Frame that shows the current lighting. Call with _state_lock held

        Black while suspended or off, the colour if static. None if nothing was chosen
        yet, even while suspended: the headset then shows its own wave, and that could
        not be put back after a black frame.

        :return: The frame, None if the daemon has nothing to show
        :rtype: bytes or None
        """
        effect = self.zone['backlight']['effect']

        if effect not in ('static', 'none'):
            return None
        if self._lights_suspended or effect == 'none':
            return _protocol.build_color(0, 0, 0)
        return _protocol.build_color(*self.zone['backlight']['colors'][0:3])

    def _suspend_device(self):
        """
        Screen locked: black, without touching the chosen lighting or the saved brightness

        Only over a lighting chosen in the daemon, see _lighting_frame.
        """
        with self._write_lock:
            with self._state_lock:
                self._lights_suspended = True
                frame = self._lighting_frame() if self._linked() else None

            if frame is not None:
                self._transport.write(frame)

    def _resume_device(self):
        """
        Screen unlocked: show the chosen lighting again
        """
        with self._write_lock:
            with self._state_lock:
                self._lights_suspended = False
                frame = self._lighting_frame() if self._linked() else None

            if frame is not None:
                self._transport.write(frame)

    def _close(self):
        super()._close()

        with self._state_lock:
            self._closing = True
            sync_thread = self._sync_thread

        if self._transport is not None:
            self._transport.close()

        # The transport is closed, so a running sync gets no more replies and ends at once
        if sync_thread is not None and sync_thread is not threading.current_thread():
            sync_thread.join(self.SYNC_JOIN_TIMEOUT)
