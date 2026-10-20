# SPDX-License-Identifier: GPL-2.0-or-later

import os
import shutil
import tempfile
import unittest
from unittest import mock

import openrazer_daemon.hardware
from openrazer_daemon.hardware.device_base import RazerDevice
from openrazer_daemon.hardware.hidraw_devices import _RazerHidrawDevice, RazerBarracudaXChromaWired, RazerBarracudaXChromaWireless, RazerSeirenV3Mini

from hidraw_fixtures import build_device, make_sysfs_hid

# class, PID, sysfs name, device name, device type, serial
PASSIVE_DEVICES = (
    (RazerBarracudaXChromaWired, 0x0573, '0003:1532:0573.0015', 'Razer Barracuda X Chroma (Wired)', 'headset', 'HIDRAW15320573'),
    (RazerSeirenV3Mini, 0x056A, '0003:1532:056A.0003', 'Razer Seiren V3 Mini', 'accessory', 'HIDRAW1532056A'),
)


class HidrawDevicesTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='test_hidraw_devices_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_match_rules(self):
        for cls, pid, sys_name, _name, _type, _serial in PASSIVE_DEVICES:
            with self.subTest(cls=cls.__name__):
                good = make_sysfs_hid(self.root, sys_name)
                self.assertTrue(cls.match(sys_name, good))

                # The kernel driver is bound: OpenRazer's own classes handle that device
                with_type = make_sysfs_hid(self.root, sys_name[:-4] + '0099', device_type=True)
                self.assertFalse(cls.match(os.path.basename(with_type), with_type))

                # No hidraw/ child
                no_hidraw = make_sysfs_hid(self.root, sys_name[:-4] + '0098', hidraw_name=None)
                self.assertFalse(cls.match(os.path.basename(no_hidraw), no_hidraw))

                # hidraw/ exists but is empty
                empty = make_sysfs_hid(self.root, sys_name[:-4] + '0097')
                os.rmdir(os.path.join(empty, 'hidraw', 'hidraw900'))
                self.assertFalse(cls.match(os.path.basename(empty), empty))

                # Not a USB HID bus
                self.assertFalse(cls.match('0005' + sys_name[4:], good))

                # Wrong vendor, wrong product, trailing garbage
                self.assertFalse(cls.match('0003:1533' + sys_name[9:], good))
                self.assertFalse(cls.match(sys_name[:10] + '0000' + sys_name[14:], good))
                self.assertFalse(cls.match(sys_name + '\n', good))
                self.assertFalse(cls.match(sys_name + '0', good))

                # A path that does not exist is a plain no
                self.assertFalse(cls.match(sys_name, os.path.join(self.root, 'does-not-exist')))

    def test_match_uses_the_class_product_id(self):
        sys_name = '0003:1532:0574.0015'
        dev_path = make_sysfs_hid(self.root, sys_name)
        self.assertTrue(RazerBarracudaXChromaWireless.match(sys_name, dev_path))
        for cls in (RazerBarracudaXChromaWired, RazerSeirenV3Mini):
            with self.subTest(cls=cls.__name__):
                self.assertFalse(cls.match(sys_name, dev_path))

        # And the other way round
        for other in ('0003:1532:0573.0016', '0003:1532:056A.0017'):
            with self.subTest(other=other):
                self.assertFalse(RazerBarracudaXChromaWireless.match(other, make_sysfs_hid(self.root, other)))

    def test_match_requires_upper_case_hex_for_seiren(self):
        dev_path = make_sysfs_hid(self.root, '0003:1532:056A.0003')
        self.assertTrue(RazerSeirenV3Mini.match('0003:1532:056A.0003', dev_path))
        self.assertFalse(RazerSeirenV3Mini.match('0003:1532:056a.0003', dev_path))

    def test_match_leaves_kernel_driver_devices_alone(self):
        # RazerDevice.match is untouched: it still wants a device_type file
        dev_path = make_sysfs_hid(self.root, '0003:1532:0573.0015')

        class Plain(RazerDevice):
            USB_VID = 0x1532
            USB_PID = 0x0573

        self.assertFalse(Plain.match('0003:1532:0573.0015', dev_path))
        with_type = make_sysfs_hid(self.root, '0003:1532:0573.0016', device_type=True)
        self.assertTrue(Plain.match('0003:1532:0573.0016', with_type))

    def test_hidraw_node(self):
        dev_path = make_sysfs_hid(self.root, '0003:1532:056A.0003', hidraw_name='hidraw900')
        self.assertEqual(_RazerHidrawDevice.hidraw_node(dev_path), '/dev/hidraw900')
        self.assertEqual(RazerSeirenV3Mini.hidraw_node(dev_path), '/dev/hidraw900')

        # The first entry in sorted order wins
        os.makedirs(os.path.join(dev_path, 'hidraw', 'hidraw12'))
        os.makedirs(os.path.join(dev_path, 'hidraw', 'hidraw901'))
        self.assertEqual(_RazerHidrawDevice.hidraw_node(dev_path), '/dev/hidraw12')

    def test_hidraw_node_without_hidraw(self):
        no_hidraw = make_sysfs_hid(self.root, '0003:1532:056A.0004', hidraw_name=None)
        self.assertIsNone(_RazerHidrawDevice.hidraw_node(no_hidraw))
        self.assertIsNone(_RazerHidrawDevice.hidraw_node(os.path.join(self.root, 'does-not-exist')))

    def test_check_access(self):
        dev_path = make_sysfs_hid(self.root, '0003:1532:056A.0003')

        for cls, _pid, _sys_name, _name, _type, _serial in PASSIVE_DEVICES:
            with self.subTest(cls=cls.__name__, testing=True):
                with mock.patch('os.access') as os_access:
                    self.assertTrue(cls.check_access(dev_path, True))
                os_access.assert_not_called()

        # A passive device never opens the node, so it does not need to be allowed to
        self.assertFalse(RazerSeirenV3Mini.OPENS_HIDRAW)
        with mock.patch('os.access') as os_access:
            self.assertTrue(RazerSeirenV3Mini.check_access(dev_path, False))
        os_access.assert_not_called()

    def test_check_access_for_devices_that_open_the_node(self):
        dev_path = make_sysfs_hid(self.root, '0003:1532:056A.0003')

        class Opener(_RazerHidrawDevice):
            USB_PID = 0x056A
            OPENS_HIDRAW = True

        with mock.patch('os.access', return_value=True) as os_access:
            self.assertTrue(Opener.check_access(dev_path, False))
        os_access.assert_called_once_with('/dev/hidraw900', os.R_OK | os.W_OK)

        with mock.patch('os.access', return_value=False) as os_access:
            self.assertFalse(Opener.check_access(dev_path, False))
        os_access.assert_called_once_with('/dev/hidraw900', os.R_OK | os.W_OK)

        # The test directory never holds a real node
        with mock.patch('os.access', return_value=False) as os_access:
            self.assertTrue(Opener.check_access(dev_path, True))
        os_access.assert_not_called()

        # No hidraw/ entry, nothing to open
        no_hidraw = make_sysfs_hid(self.root, '0003:1532:056A.0004', hidraw_name=None)
        with mock.patch('os.access', return_value=True) as os_access:
            self.assertFalse(Opener.check_access(no_hidraw, False))
        os_access.assert_not_called()

    def test_default_check_access_keeps_the_group_rules(self):
        dev_path = make_sysfs_hid(self.root, '0003:1532:0573.0015', device_type=True)

        # The file was created by this user, so it belongs to one of the user's groups
        self.assertTrue(RazerDevice.check_access(dev_path, False))
        self.assertTrue(RazerDevice.check_access(dev_path, True))

        other_gid = os.getgid() + 1
        with mock.patch('os.stat', return_value=mock.Mock(st_gid=other_gid)):
            with mock.patch('grp.getgrgid', return_value=('nobody', 'x', other_gid, [])):
                self.assertFalse(RazerDevice.check_access(dev_path, False))
            with mock.patch('grp.getgrgid', return_value=('plugdev', 'x', other_gid, [])):
                self.assertTrue(RazerDevice.check_access(dev_path, False))

    def test_default_access_denied_message_is_unchanged(self):
        self.assertEqual(RazerDevice.access_denied_message('/sys/bus/hid/devices/0003:1532:0084.0001'),
                         'Could not access /sys/bus/hid/devices/0003:1532:0084.0001/device_type, file is not owned by plugdev')

    def test_hidraw_access_denied_message_names_the_node_and_the_rule(self):
        dev_path = make_sysfs_hid(self.root, '0003:1532:0574.0001', hidraw_name='hidraw900')
        self.assertEqual(RazerBarracudaXChromaWireless.access_denied_message(dev_path),
                         'Could not open /dev/hidraw900 for Razer Barracuda X Chroma (Wireless): '
                         'check install_files/udev/70-openrazer-hidraw-local.rules (group plugdev / uaccess)')

        # The node went away between match() and the check
        no_hidraw = make_sysfs_hid(self.root, '0003:1532:0574.0002', hidraw_name=None)
        self.assertEqual(RazerBarracudaXChromaWireless.access_denied_message(no_hidraw),
                         'Could not find the hidraw node of {0} for Razer Barracuda X Chroma (Wireless)'.format(no_hidraw))

    def test_load_devices_logs_the_class_access_message(self):
        from openrazer_daemon.daemon import RazerDaemon

        dev_path = make_sysfs_hid(self.root, '0003:1532:0574.0001')
        device_class = mock.MagicMock()
        device_class.match.return_value = True
        device_class.check_access.return_value = False
        device_class.access_denied_message.return_value = 'denied by the class'
        daemon = mock.MagicMock(_test_dir=self.root, _device_classes=[device_class], _razer_devices=[])

        RazerDaemon._load_devices(daemon)

        device_class.check_access.assert_called_once_with(dev_path, True)
        device_class.access_denied_message.assert_called_once_with(dev_path)
        daemon.logger.critical.assert_called_once_with('denied by the class')
        # Not built
        device_class.assert_not_called()

    def test_add_device_ignores_udev_events_in_test_mode(self):
        from openrazer_daemon.daemon import RazerDaemon

        udev_device = mock.Mock(sys_name='0003:1532:0084.0001', sys_path='/sys/bus/hid/devices/0003:1532:0084.0001')
        for test_dir, built in ((self.root, False), (None, True)):
            with self.subTest(test_dir=test_dir):
                # Kernel driver class or hidraw class alike
                device_class = mock.MagicMock()
                device_class.match.return_value = True
                device_class.HIDRAW_DEVICE = False
                daemon = mock.MagicMock(_test_dir=test_dir, _device_classes=[device_class], _razer_devices=[])

                with mock.patch('time.sleep'), mock.patch('logging.warning'):
                    RazerDaemon._add_device(daemon, udev_device)

                self.assertEqual(device_class.called, built)
                self.assertEqual(device_class.match.called, built)

    def test_passive_devices_publish_misc_surface(self):
        for cls, pid, sys_name, name, dev_type, serial in PASSIVE_DEVICES:
            with self.subTest(cls=cls.__name__):
                dev = build_device(cls, make_sysfs_hid(self.root, sys_name))
                self.addCleanup(dev.close)

                self.assertEqual(dev.get_serial(), serial)
                self.assertEqual(dev.getSerial(), serial)
                self.assertEqual(dev.getDeviceName(), name)
                self.assertEqual(dev.getFirmware(), 'unknown')
                self.assertEqual(dev.getDeviceType(), dev_type)
                self.assertEqual(dev.get_vid_pid(), [0x1532, pid])
                self.assertEqual(dev.getVidPid(), [0x1532, pid])
                self.assertIsInstance(dev.get_device_image(), str)
                self.assertIsInstance(dev.getDeviceImage(), str)
                self.assertEqual(dev.getDeviceMode(), '0:0')

    def test_passive_devices_have_no_side_effects_on_mode_and_brightness(self):
        for cls, _pid, sys_name, _name, _type, _serial in PASSIVE_DEVICES:
            with self.subTest(cls=cls.__name__):
                dev = build_device(cls, make_sysfs_hid(self.root, sys_name))
                self.addCleanup(dev.close)

                dev.set_device_mode(3, 0)
                self.assertEqual(dev.get_device_mode(), '0:0')
                self.assertIsNone(dev.restore_brightness())
                self.assertIsNone(dev.disable_brightness())
                # Suspend and resume go through the same hooks and must not raise
                dev.suspend_device()
                dev.resume_device()

    def test_passive_devices_never_open_node(self):
        for cls, _pid, sys_name, _name, _type, _serial in PASSIVE_DEVICES:
            with self.subTest(cls=cls.__name__):
                dev_path = make_sysfs_hid(self.root, sys_name)
                with mock.patch('os.open') as os_open:
                    dev = build_device(cls, dev_path)
                    self.addCleanup(dev.close)
                    dev.getDeviceName()
                    dev.getFirmware()
                    dev.suspend_device()
                    dev.resume_device()
                os_open.assert_not_called()

    def test_extra_dbus_methods_hook_is_empty_by_default(self):
        dev = build_device(RazerSeirenV3Mini, make_sysfs_hid(self.root, '0003:1532:056A.0003'))
        self.addCleanup(dev.close)
        self.assertEqual(dev._extra_dbus_methods(), [])

    def test_classes_registered(self):
        names = [cls.__name__ for cls in openrazer_daemon.hardware.get_device_classes()]
        self.assertIn('RazerBarracudaXChromaWired', names)
        self.assertIn('RazerBarracudaXChromaWireless', names)
        self.assertIn('RazerSeirenV3Mini', names)
        self.assertNotIn('_RazerHidrawDevice', names)

        classes = openrazer_daemon.hardware.get_device_classes()
        self.assertIn(RazerBarracudaXChromaWired, classes)
        self.assertIn(RazerBarracudaXChromaWireless, classes)
        self.assertIn(RazerSeirenV3Mini, classes)
        self.assertNotIn(_RazerHidrawDevice, classes)
        self.assertNotIn(RazerDevice, classes)

    def test_only_hidraw_classes_are_marked_hidraw(self):
        for cls in openrazer_daemon.hardware.get_device_classes():
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls.HIDRAW_DEVICE, issubclass(cls, _RazerHidrawDevice))
        self.assertFalse(RazerDevice.HIDRAW_DEVICE)


if __name__ == '__main__':
    unittest.main()
