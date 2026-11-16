import unittest
from types import SimpleNamespace
from unittest.mock import patch
import ats_monitor_ui as ui


class PollResilienceTests(unittest.TestCase):
    def test_transient_failure_keeps_only_fresh_status(self):
        status = {'online': True, 'mode': 'manual', 'output_speed': 0}
        app = SimpleNamespace(fan_endpoint='http://example', _fan_busy=False,
                              fan_status=status, _fan_last_poll_at=100.0,
                              update_fan_dialog=lambda: None,
                              schedule_after=lambda *args: None)
        app.poll_fan = lambda: None
        app.fan_request = lambda operation, done: done(False, 'timeout')
        with patch.object(ui.time, 'monotonic', return_value=105.0):
            ui.ATSApp.poll_fan(app)
        self.assertEqual(app.fan_status, status)
        with patch.object(ui.time, 'monotonic', return_value=113.0):
            ui.ATSApp.poll_fan(app)
        self.assertIsNone(app.fan_status)

    def test_busy_request_still_expires(self):
        app = SimpleNamespace(fan_endpoint='http://example', _fan_busy=True,
                              fan_status={'online': True}, _fan_last_poll_at=100.0,
                              update_fan_dialog=lambda: None,
                              schedule_after=lambda *args: None, poll_fan=lambda: None)
        with patch.object(ui.time, 'monotonic', return_value=113.0):
            ui.ATSApp.poll_fan(app)
        self.assertIsNone(app.fan_status)

    def test_generation_rejects_old_request(self):
        callbacks = []
        app = SimpleNamespace(fan_endpoint='http://example', _fan_busy=False,
                              _fan_generation=1, _fan_last_poll_at=0,
                              safe_after_from_worker=callbacks.append)
        class ImmediateThread:
            def __init__(self, target, daemon): self.target = target
            def start(self): self.target()
        with patch.object(ui.threading, 'Thread', ImmediateThread):
            ui.ATSApp.fan_request(app, lambda client: {'online': True}, lambda *args: self.fail('stale callback'))
        app._fan_generation = 2
        callbacks.pop()()
        self.assertEqual(app._fan_last_poll_at, 0)
        self.assertFalse(app._fan_busy)

    def test_delayed_callback_does_not_refresh_stale_response(self):
        callbacks = []
        app = SimpleNamespace(fan_endpoint='http://example', _fan_busy=False,
                              _fan_generation=1, _fan_last_poll_at=0, fan_status=None,
                              update_fan_dialog=lambda: None, schedule_after=lambda *args: None,
                              poll_fan=lambda: None, safe_after_from_worker=callbacks.append)
        class ImmediateThread:
            def __init__(self, target, daemon): self.target = target
            def start(self): self.target()
        app.fan_request = lambda operation, done: ui.ATSApp.fan_request(app, lambda client: {'online': True, 'mode': 'auto', 'output_speed': 0}, done)
        with patch.object(ui.threading, 'Thread', ImmediateThread), patch.object(ui.time, 'monotonic', return_value=100):
            ui.ATSApp.poll_fan(app)
        with patch.object(ui.time, 'monotonic', return_value=113):
            callbacks.pop()()
        self.assertIsNone(app.fan_status)

if __name__ == '__main__': unittest.main()
