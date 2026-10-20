# SPDX-License-Identifier: GPL-2.0-or-later

import inspect
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from openrazer_daemon.hardware.hidraw_devices import RazerBarracudaXChromaWireless
from openrazer_daemon.misc.barracuda_protocol import build_color, build_get, build_set, check_frame
from openrazer_daemon.misc.battery_notifier import BatteryNotifier

from fake_hidraw import FakeDongle, make_wireless
from hidraw_fixtures import build_device, make_config, make_sysfs_hid

TIMEOUT = 2.0

# How long the device waits for the reply to a GET (HidrawTransport.request)
REPLY_TIMEOUT = 1.5

BLACK = build_color(0, 0, 0)
BLUE = build_color(0, 0, 255)

# What the device reads every time the link comes up, in this order
SYNC_GETS = [build_get(0x20), build_get(0x21), build_get(0x2A), build_get(0x2C), build_get(0x41)]

# What write_sentinel() writes: idle time 300 s, i.e. 5 minutes
SENTINEL = build_set(0x2C, 5)


def is_color(frame):
    return frame[7:9] == b'\x0f\x03'


def is_set_brightness(frame):
    return frame[7:9] == b'\x00\x00' and frame[10] == 0xC1


class BarracudaWirelessTestCase(unittest.TestCase):
    def setUp(self):
        self.dongles = []

    def tearDown(self):
        # Every frame that reached a fake dongle must have passed the safety gate
        for dongle in self.dongles:
            self.assertEqual(dongle.rejected, [])

    def wait_until(self, predicate, what, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('timed out waiting for ' + what)
            time.sleep(0.01)

    def make(self, dongle=None, config=None):
        """Build the device on a fake dongle, return both"""
        if dongle is None:
            dongle = FakeDongle()
        self.dongles.append(dongle)
        return make_wireless(self, dongle, config), dongle

    def make_synced(self, dongle=None):
        """Build the device and wait until the sync done at construction finished"""
        dev, dongle = self.make(dongle)
        self.wait_for_sync(dev)
        self.assertEqual(dongle.writes, SYNC_GETS)
        return dev, dongle

    def wait_for_sync(self, dev):
        self.wait_until(lambda: dev._sync_thread is None, 'the sync to finish')

    def wait_for_write(self, dongle, frame, start=0, timeout=TIMEOUT):
        self.wait_until(lambda: frame in dongle.writes[start:], 'frame ' + frame.hex(), timeout)

    def wait_last_write(self, dongle, frame):
        self.wait_until(lambda: dongle.writes[-1:] == [frame], 'last frame ' + frame.hex())

    def write_sentinel(self, dev, dongle, start):
        """
        Write a known frame and wait for it. Frames travel in order over one socket, so
        once it arrived everything written before it arrived too
        """
        dev.setIdleTime(300)
        self.wait_for_write(dongle, SENTINEL, start)

    def notifier_config(self):
        """José's configuration: battery notifications every 600 s at 33 % or less"""
        config = make_config()
        config['Startup']['battery_notifier'] = 'True'
        config['Startup']['battery_notifier_freq'] = '600'
        config['Startup']['battery_notifier_percent'] = '33'
        return config

    def patch_notifications(self):
        """Replace the desktop notification. Call before make(): the device is closed before the patch ends"""
        patcher = mock.patch.object(BatteryNotifier, 'show_notification')
        show = patcher.start()
        self.addCleanup(patcher.stop)
        return show

    def link_cycle(self, dev, dongle):
        """Turn the headset off and on, wait until the sync that follows finished, return the index of its first write"""
        start = len(dongle.writes)
        dongle.emit_event(0x20, 0)
        dongle.emit_event(0x20, 1)
        self.wait_for_write(dongle, build_get(0x20), start)
        self.wait_for_sync(dev)
        return start

    def test_startup_sync_fills_cache(self):
        dev, dongle = self.make()

        self.wait_until(lambda: dev.getBattery() == 79.0, 'battery 79')
        self.assertIsInstance(dev.getBattery(), float)
        self.assertIs(dev.isCharging(), False)
        self.wait_until(lambda: dev.getIdleTime() == 300, 'idle time 300')
        self.wait_until(lambda: dev.getBrightness() == 32.9, 'brightness 32.9')
        self.wait_for_sync(dev)

        # The getters never talk to the device: the sentinel is the next frame the dongle sees
        for _ in range(3):
            dev.getBattery()
            dev.isCharging()
            dev.getIdleTime()
            dev.getBrightness()
        self.write_sentinel(dev, dongle, len(SYNC_GETS))

        # Only reads at startup: no brightness SET, and no colour since nothing was chosen yet
        self.assertEqual(dongle.writes, SYNC_GETS + [SENTINEL])
        self.assertEqual([f for f in dongle.writes if is_set_brightness(f)], [])
        self.assertFalse(dev.persistence.status['changed'])

    def test_battery_and_charging_events(self):
        dev, dongle = self.make_synced()

        dongle.emit_event(0x21, 94)
        self.wait_until(lambda: dev.getBattery() == 94.0, 'battery 94')

        dongle.emit_event(0x2A, 1)
        self.wait_until(lambda: dev.isCharging() is True, 'charging')

        dongle.emit_event(0x20, 0)
        self.wait_until(lambda: dev.isCharging() is False, 'not charging after link down')
        self.assertEqual(dev.getBattery(), 94.0)

        # Without link it is never "charging", whatever was reported last
        dongle.emit_event(0x2A, 1)
        dongle.emit_event(0x21, 50)
        self.wait_until(lambda: dev.getBattery() == 50.0, 'battery 50')
        self.assertIs(dev.isCharging(), False)

    def test_charging_is_forgotten_when_the_link_goes_down(self):
        dev, dongle = self.make_synced(FakeDongle(props={0x20: 1, 0x21: 79, 0x2A: 1, 0x2C: 5, 0x41: 84}))
        self.assertIs(dev.isCharging(), True)

        dongle.emit_event(0x20, 0)
        self.wait_until(lambda: not dev._link, 'link down')
        # Nothing is in flight now. From here on the dongle does not answer GET 0x2A,
        # so after the link comes back nobody has said the headset charges
        del dongle.props[0x2A]
        start = len(dongle.writes)
        dongle.emit_event(0x20, 1)

        # The sync of the new link is waiting for the charging state
        self.wait_for_write(dongle, build_get(0x2A), start)
        self.assertTrue(dev._link)
        self.assertIs(dev.isCharging(), False)

    def test_lost_link_reply_does_not_cancel_a_link_event(self):
        dev, dongle = self.make_synced()
        dev.setStatic(0, 0, 255)
        self.wait_last_write(dongle, BLUE)
        dongle.emit_event(0x20, 0)
        self.wait_until(lambda: not dev._link, 'link down')

        # The headset says the link is up, but the reply to the GET 0x20 that follows is lost
        dongle.silent_props.add(0x20)
        start = len(dongle.writes)
        dongle.emit_event(0x20, 1)

        # The GET waits for its whole timeout, then the sync goes on and applies the colour
        self.wait_for_write(dongle, BLUE, start, REPLY_TIMEOUT + TIMEOUT)
        self.wait_for_sync(dev)
        self.assertTrue(dev._link)
        self.assertEqual(dongle.writes[start:], SYNC_GETS + [BLUE])

        # And the setters still write
        dev.setStatic(255, 0, 0)
        self.wait_last_write(dongle, build_color(255, 0, 0))

    def test_battery_notifier_only_runs_while_linked(self):
        show = self.patch_notifications()
        dev, dongle = self.make(FakeDongle(props={0x20: 0, 0x21: 20, 0x2A: 0, 0x2C: 5, 0x41: 84}), self.notifier_config())
        self.wait_for_sync(dev)
        # An explicit "not linked" reply ends the sync at once
        self.assertEqual(dongle.writes, [build_get(0x20)])
        self.assertFalse(dev._battery_manager.active)

        # A level heard while the link is down is the last known one for D-Bus, never a notification
        dongle.emit_event(0x21, 20)
        self.wait_until(lambda: dev.getBattery() == 20.0, 'battery 20')
        time.sleep(0.3)  # a few rounds of the notifier thread
        self.assertFalse(dev._battery_manager.active)
        show.assert_not_called()

        # Linked, the sync reads the level and the notifier warns about it
        dongle.emit_event(0x20, 1)
        self.wait_until(lambda: show.called, 'the low battery notification')
        show.assert_called_once_with(summary='Razer Barracuda X Chroma (Wireless)', message='Battery is 20%', icon='battery-low')

        # Link down: off at once, and closing does not wait for the notifier
        dongle.emit_event(0x20, 0)
        self.wait_until(lambda: not dev._battery_manager.active, 'the notifier to stop')
        started = time.monotonic()
        dev.close()
        self.assertLess(time.monotonic() - started, 1.0)
        show.assert_called_once()

    def test_battery_notifier_waits_for_a_level_read_on_the_link(self):
        show = self.patch_notifications()
        dev, dongle = self.make(FakeDongle(props={0x20: 1, 0x21: 20, 0x2A: 0, 0x2C: 5, 0x41: 84}), self.notifier_config())
        self.wait_until(lambda: show.called, 'the low battery notification')
        self.assertTrue(dev._battery_manager.active)
        dongle.emit_event(0x20, 0)
        self.wait_until(lambda: not dev._battery_manager.active, 'the notifier to stop')

        # Back on the link, the reply to GET 0x21 gets lost: the level of the last link is not trusted
        dongle.silent_props.add(0x21)
        start = len(dongle.writes)
        dongle.emit_event(0x20, 1)
        self.wait_for_write(dongle, build_get(0x41), start, REPLY_TIMEOUT + TIMEOUT)
        self.wait_for_sync(dev)
        self.assertTrue(dev._link)
        self.assertEqual(dev.getBattery(), 20.0)
        self.assertFalse(dev._battery_manager.active)

        # A battery event on the link is a fresh level
        dongle.emit_event(0x21, 25)
        self.wait_until(lambda: dev._battery_manager.active, 'the notifier to start')
        self.assertEqual(dev.getBattery(), 25.0)

    def test_battery_notifier_stays_off_while_nothing_is_known(self):
        show = self.patch_notifications()
        dev, dongle = self.make(FakeDongle(silent=True), self.notifier_config())

        time.sleep(0.3)  # a few rounds of the notifier thread
        self.assertFalse(dev._battery_manager.active)
        # No 10-s retry on the unknown level, so closing is quick
        started = time.monotonic()
        dev.close()
        self.assertLess(time.monotonic() - started, 1.0)
        show.assert_not_called()

    def test_battery_notifier_respects_the_configuration(self):
        dev, dongle = self.make_synced()  # battery_notifier = False
        self.assertFalse(dev._battery_manager.active)
        self.link_cycle(dev, dongle)
        self.assertFalse(dev._battery_manager.active)

    def test_set_static_writes_color_and_persists(self):
        dev, dongle = self.make_synced()
        observer = mock.Mock()
        dev.register_observer(observer)

        dev.setStatic(0, 0, 255)
        self.wait_last_write(dongle, BLUE)

        self.assertEqual(dev.getEffect(), 'static')
        self.assertEqual(dev.zone['backlight']['colors'][0:3], [0, 0, 255])
        self.assertTrue(dev.persistence.status['changed'])
        observer.notify.assert_called_once_with(('effect', dev, 'setStatic', 0, 0, 255))

    def test_set_none_writes_black(self):
        dev, dongle = self.make_synced()
        dev.setStatic(0, 0, 255)
        self.wait_last_write(dongle, BLUE)
        observer = mock.Mock()
        dev.register_observer(observer)

        dev.setNone()
        self.wait_last_write(dongle, BLACK)

        self.assertEqual(dev.getEffect(), 'none')
        # The colour stays for the next time
        self.assertEqual(dev.zone['backlight']['colors'][0:3], [0, 0, 255])
        observer.notify.assert_called_once_with(('effect', dev, 'setNone'))
        # Off is a black colour frame, never brightness 0
        self.assertEqual([f for f in dongle.writes if is_set_brightness(f)], [])

    def test_set_brightness_converts_and_persists(self):
        dev, dongle = self.make_synced()
        observer = mock.Mock()
        dev.register_observer(observer)

        dev.setBrightness(50)
        self.wait_last_write(dongle, build_set(0x41, 128))
        self.assertEqual(dev.getBrightness(), 50.0)
        self.assertTrue(dev.persistence.status['changed'])

        dev.setBrightness(150)
        self.wait_last_write(dongle, build_set(0x41, 255))
        self.assertEqual(dev.getBrightness(), 100.0)

        dev.setBrightness(-5)
        self.wait_last_write(dongle, build_set(0x41, 0))
        self.assertEqual(dev.getBrightness(), 0.0)

        # Brightness is not synced to other devices
        observer.notify.assert_not_called()

    def test_pending_brightness_applied_on_link_up(self):
        dev, dongle = self.make_synced()
        dongle.emit_event(0x20, 0)
        self.wait_until(lambda: not dev._link, 'link down')
        start = len(dongle.writes)

        dev.setBrightness(50)
        self.assertEqual(dev.getBrightness(), 50.0)
        dongle.emit_event(0x20, 1)
        self.wait_for_write(dongle, build_set(0x41, 128), start)
        self.wait_for_sync(dev)

        # José's value wins over the 84 the headset still had when it was read
        self.assertEqual(dev.getBrightness(), 50.0)
        self.assertEqual(dongle.props[0x41], 128)
        # Nothing went out while the link was down, and the change is written once
        self.write_sentinel(dev, dongle, start)
        self.assertEqual(dongle.writes[start:], SYNC_GETS + [build_set(0x41, 128), SENTINEL])

        # The next link up only reads it again
        start = self.link_cycle(dev, dongle)
        self.write_sentinel(dev, dongle, start)
        self.assertEqual(dongle.writes[start:], SYNC_GETS + [SENTINEL])
        self.assertEqual(dev.getBrightness(), 50.2)

    def test_idle_time_conversion(self):
        dev, dongle = self.make_synced()

        dev.setIdleTime(420)
        self.wait_last_write(dongle, build_set(0x2C, 7))
        self.assertEqual(dev.getIdleTime(), 420)

        dev.setIdleTime(0)
        self.wait_last_write(dongle, build_set(0x2C, 0))
        self.assertEqual(dev.getIdleTime(), 0)

        dev.setIdleTime(7200)
        self.wait_last_write(dongle, build_set(0x2C, 60))
        self.assertEqual(dev.getIdleTime(), 3600)

    def test_link_up_reapplies_static(self):
        dev, dongle = self.make_synced()
        dev.setStatic(0, 0, 255)
        self.wait_last_write(dongle, BLUE)

        start = self.link_cycle(dev, dongle)
        self.wait_for_write(dongle, BLUE, start)

        self.assertEqual(dongle.writes[-1], BLUE)
        # Reads, then the colour once; the brightness stays as the headset saved it
        self.assertEqual(dongle.writes[start:], SYNC_GETS + [BLUE])

    def test_no_reapply_without_user_choice(self):
        dev, dongle = self.make_synced()
        self.assertEqual(dev.getEffect(), 'spectrum')

        start = self.link_cycle(dev, dongle)
        self.write_sentinel(dev, dongle, start)

        self.assertEqual(dongle.writes[start:], SYNC_GETS + [SENTINEL])
        self.assertEqual([f for f in dongle.writes if is_color(f)], [])

    def test_link_up_while_suspended_sends_black(self):
        dev, dongle = self.make_synced()
        dev.setStatic(0, 0, 255)
        self.wait_last_write(dongle, BLUE)

        dev.suspend_device()
        self.wait_last_write(dongle, BLACK)

        start = self.link_cycle(dev, dongle)
        self.wait_for_write(dongle, BLACK, start)
        self.assertEqual([f for f in dongle.writes[start:] if is_color(f)], [BLACK])

        dev.resume_device()
        self.wait_last_write(dongle, BLUE)

        self.assertEqual(dev.zone['backlight']['colors'][0:3], [0, 0, 255])
        self.assertEqual(dev.getEffect(), 'static')
        # The screen lock never touches the brightness saved in the headset
        self.assertEqual([f for f in dongle.writes if is_set_brightness(f)], [])

    def test_lock_without_user_choice_leaves_the_headset_alone(self):
        # Nothing chosen yet: the headset shows its own wave, which cannot be restored
        # once overwritten, so the screen lock must not write black
        dev, dongle = self.make_synced()
        self.assertEqual(dev.getEffect(), 'spectrum')
        start = len(dongle.writes)

        dev.suspend_device()
        self.assertTrue(dev._lights_suspended)
        self.link_cycle(dev, dongle)
        dev.resume_device()
        self.assertFalse(dev._lights_suspended)

        self.write_sentinel(dev, dongle, start)
        self.assertEqual(dongle.writes[start:], SYNC_GETS + [SENTINEL])
        self.assertEqual([f for f in dongle.writes if is_color(f)], [])

    def test_set_static_while_suspended_waits_for_resume(self):
        dev, dongle = self.make_synced()
        dev.setStatic(0, 0, 255)
        self.wait_last_write(dongle, BLUE)
        dev.suspend_device()
        self.wait_last_write(dongle, BLACK)
        start = len(dongle.writes)

        dev.setStatic(255, 0, 0)
        self.assertEqual(dev.getEffect(), 'static')
        # Nothing was written: the sentinel is the first frame the dongle sees
        self.write_sentinel(dev, dongle, start)
        self.assertEqual(dongle.writes[start:], [SENTINEL])

        dev.resume_device()
        self.wait_last_write(dongle, build_color(255, 0, 0))

    def test_synced_brightness_is_ignored(self):
        dev, dongle = self.make_synced()
        brightness = dev.getBrightness()
        start = len(dongle.writes)

        dev.disable_notify = True
        dev.setBrightness(10)
        dev.disable_notify = False
        # The path effect sync really takes
        dev._effect_sync.run_effect('setBrightness', 10)

        self.assertEqual(dev.getBrightness(), brightness)
        self.assertFalse(dev.persistence.status['changed'])
        # Nothing was written: the sentinel is the first frame the dongle sees
        self.write_sentinel(dev, dongle, start)
        self.assertEqual(dongle.writes[start:], [SENTINEL])

    def test_synced_static_and_none_are_applied(self):
        dev, dongle = self.make_synced()

        dev._effect_sync.run_effect('setStatic', 0, 0, 255)
        self.wait_last_write(dongle, BLUE)
        self.assertEqual(dev.getEffect(), 'static')

        dev._effect_sync.run_effect('setNone')
        self.wait_last_write(dongle, BLACK)
        self.assertEqual(dev.getEffect(), 'none')

    def test_pending_idle_applied_on_link_up(self):
        dev, dongle = self.make_synced()
        dongle.emit_event(0x20, 0)
        self.wait_until(lambda: not dev._link, 'link down')
        start = len(dongle.writes)

        dev.setIdleTime(600)
        dongle.emit_event(0x20, 1)
        self.wait_for_write(dongle, build_set(0x2C, 10), start)

        self.wait_for_sync(dev)
        self.assertEqual(dev.getIdleTime(), 600)
        self.assertEqual(dongle.props[0x2C], 10)

        # Nothing went out while the link was down: the reads of the sync come first
        self.write_sentinel(dev, dongle, start)
        self.assertEqual(dongle.writes[start:], SYNC_GETS + [build_set(0x2C, 10), SENTINEL])

    def test_construct_with_silent_dongle_is_fast_and_safe(self):
        started = time.monotonic()
        dev, dongle = self.make(FakeDongle(silent=True))
        self.assertLess(time.monotonic() - started, 0.5)

        self.assertEqual(dev.getBattery(), -1.0)
        self.assertIs(dev.isCharging(), False)
        self.assertEqual(dev.getIdleTime(), 0)
        dev.setStatic(1, 2, 3)
        self.assertEqual(dev.getEffect(), 'static')

    def test_setters_after_disconnect_do_not_raise(self):
        dev, dongle = self.make_synced()

        dongle.close()
        self.wait_until(lambda: not dev._link, 'link down after disconnect')

        dev.setStatic(1, 2, 3)
        dev.setBrightness(20)
        dev.setIdleTime(60)
        dev.suspend_device()
        dev.resume_device()
        self.assertEqual(dev.getEffect(), 'static')
        self.assertEqual(dev.zone['backlight']['colors'][0:3], [1, 2, 3])
        self.assertEqual(dev.getBrightness(), 20.0)
        self.assertIs(dev.isCharging(), False)
        # Last known value
        self.assertEqual(dev.getBattery(), 79.0)

    def test_setters_with_dead_transport_do_not_raise(self):
        # The node went away but the device has not noticed yet: writes fail, nothing raises
        dev, dongle = self.make_synced()
        dev._transport.close()

        dev.setStatic(1, 2, 3)
        dev.setNone()
        dev.setBrightness(20)
        dev.setIdleTime(60)
        dev.suspend_device()
        dev.resume_device()
        self.assertEqual(dev.getEffect(), 'none')
        self.assertEqual(dev.getBrightness(), 20.0)
        self.assertEqual(dongle.writes, SYNC_GETS)

    def test_open_failure_still_publishes_device(self):
        root = tempfile.mkdtemp(prefix='test_barracuda_wireless_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        dev_path = make_sysfs_hid(root, '0003:1532:0574.0001')

        with self.assertLogs('razer.device0', level='WARNING') as logs:
            dev = build_device(RazerBarracudaXChromaWireless, dev_path)
        self.addCleanup(dev.close)
        self.assertIn('fake_hidraw_socket', '\n'.join(logs.output))

        self.assertEqual(dev.getBattery(), -1.0)
        self.assertIs(dev.isCharging(), False)
        self.assertEqual(dev.getIdleTime(), 0)
        self.assertEqual(dev.getDeviceName(), 'Razer Barracuda X Chroma (Wireless)')
        self.assertEqual(dev.getDeviceType(), 'headset')
        self.assertEqual(dev.getSerial(), 'HIDRAW15320574')
        self.assertEqual(dev.getFirmware(), 'unknown')
        self.assertIsInstance(dev.getDeviceImage(), str)

        dev.setStatic(1, 2, 3)
        dev.setNone()
        dev.setBrightness(50)
        dev.setIdleTime(300)
        dev.suspend_device()
        dev.resume_device()
        self.assertEqual(dev.getEffect(), 'none')

    def test_open_fd_uses_the_hidraw_node_outside_testing(self):
        root = tempfile.mkdtemp(prefix='test_barracuda_wireless_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        dev_path = make_sysfs_hid(root, '0003:1532:0574.0001', hidraw_name='hidraw900')
        with self.assertLogs('razer.device0', level='WARNING'):
            dev = build_device(RazerBarracudaXChromaWireless, dev_path)
        self.addCleanup(dev.close)
        self.assertTrue(RazerBarracudaXChromaWireless.OPENS_HIDRAW)

        dev._testing = False
        with mock.patch('openrazer_daemon.misc.hidraw_transport.open_hidraw', return_value=42) as open_hidraw:
            self.assertEqual(dev._open_fd(), 42)
        open_hidraw.assert_called_once_with('/dev/hidraw900')

        shutil.rmtree(os.path.join(dev_path, 'hidraw'))
        with mock.patch('openrazer_daemon.misc.hidraw_transport.open_hidraw') as open_hidraw:
            with self.assertRaises(FileNotFoundError):
                dev._open_fd()
        open_hidraw.assert_not_called()

    def test_dbus_methods_table(self):
        dev, _dongle = self.make_synced()

        table = [(m[0], m[1], m[3], m[4]) for m in dev._extra_dbus_methods()]
        self.assertEqual(table, [
            ('razer.device.power', 'getBattery', None, 'd'),
            ('razer.device.power', 'isCharging', None, 'b'),
            ('razer.device.power', 'getIdleTime', None, 'q'),
            ('razer.device.power', 'setIdleTime', 'q', None),
            ('razer.device.lighting.chroma', 'setStatic', 'yyy', None),
            ('razer.device.lighting.chroma', 'setNone', None, None),
            ('razer.device.lighting.brightness', 'setBrightness', 'd', None),
        ])

        # D-Bus refuses a method whose arguments do not match its in-signature,
        # and effect sync / restore_effect dispatch on the argument count
        for interface, name, callback, in_sig, _out_sig in dev._extra_dbus_methods():
            with self.subTest(method=name):
                self.assertEqual(len(inspect.signature(callback).parameters), len(in_sig or ''))
                self.assertEqual(len(inspect.signature(getattr(dev, name)).parameters), len(in_sig or ''))

    def test_every_written_frame_passes_gate(self):
        dev, dongle = self.make_synced()

        dev.setStatic(10, 20, 30)
        dev.setNone()
        dev.setBrightness(33)
        dev.setIdleTime(900)
        dev.suspend_device()
        dev.resume_device()
        dev.setIdleTime(4000)
        self.link_cycle(dev, dongle)
        dev.setStatic(255, 255, 255)
        self.wait_last_write(dongle, build_color(255, 255, 255))

        for frame in dongle.writes:
            self.assertIsNone(check_frame(frame), frame.hex())
        self.assertEqual(dongle.rejected, [])


if __name__ == '__main__':
    unittest.main()
