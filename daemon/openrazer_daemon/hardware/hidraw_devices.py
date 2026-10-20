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
from typing import Optional

from openrazer_daemon.hardware.device_base import RazerDevice as _RazerDevice


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
