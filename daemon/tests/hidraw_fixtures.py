# SPDX-License-Identifier: GPL-2.0-or-later

"""
Helpers shared by the tests of the hidraw devices (no test cases in here, the
file name deliberately does not match test*.py).
"""

import configparser
import os
from typing import Optional
from unittest import mock

from openrazer_daemon.dbus_services.service import DBusService


def make_sysfs_hid(root: str, sys_name: str, hidraw_name: Optional[str] = 'hidraw900', device_type: bool = False) -> str:
    """
    Create a fake HID sysfs directory under root and return its path.

    :param root: Directory that plays the role of /sys/bus/hid/devices
    :param sys_name: Entry name, like 0003:1532:0574.0015
    :param hidraw_name: Name of the entry inside hidraw/, None to not create hidraw/ at all
    :param device_type: Create the device_type file that only the OpenRazer kernel driver provides
    """
    dev_path = os.path.join(root, sys_name)
    os.makedirs(dev_path)

    if hidraw_name is not None:
        os.makedirs(os.path.join(dev_path, 'hidraw', hidraw_name))

    if device_type:
        with open(os.path.join(dev_path, 'device_type'), 'w') as device_type_file:
            device_type_file.write('Fake device\n')

    return dev_path


def make_config() -> configparser.ConfigParser:
    """Minimal daemon configuration with every key the device constructor reads."""
    config = configparser.ConfigParser()
    config['General'] = {'verbose_logging': 'False'}
    config['Startup'] = {
        'sync_effects_enabled': 'True',
        'devices_off_on_screensaver': 'True',
        'restore_persistence': 'False',
        'persistence_dual_boot_quirk': 'False',
        'battery_notifier': 'False',
    }
    return config


def make_persistence() -> configparser.ConfigParser:
    """Empty persistence store, with the status dict the device base class writes to."""
    persistence = configparser.ConfigParser()
    persistence.status = {'changed': False}
    return persistence


def build_device(cls, dev_path: str):
    """
    Build a device without a D-Bus connection, with the same arguments daemon.py uses.

    The caller owns the result and must close() it.
    """
    with mock.patch.object(DBusService, '__init__', lambda self, object_path: None):
        return cls(device_path=dev_path, device_number=0, config=make_config(),
                   persistence=make_persistence(), testing=True,
                   additional_interfaces=[], additional_methods=[],
                   unknown_serial_counter={})
