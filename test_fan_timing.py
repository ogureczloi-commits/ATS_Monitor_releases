import unittest
from types import SimpleNamespace
from unittest.mock import patch
import ats_monitor_ui as ui
from test_gadget_fan_art import Canvas, Widget


class FanTimingTests(unittest.TestCase):
    def test_animation_uses_elapsed_time_and_stops_offline(self):
        app = SimpleNamespace(gadget=Widget(), fan_canvas=Canvas(), fan_gadget_label=Widget(),
                              _fan_angle=0.0, fan_status={'output_speed': 50, 'mode': 'auto'},
                              schedule_after=lambda *args: None)
        with patch.object(ui.time, 'monotonic', side_effect=[10, 10.1, 10.4, 10.5, 20]):
            ui.ATSApp.animate_fan(app, app.gadget)
            ui.ATSApp.animate_fan(app, app.gadget)
            first = app._fan_angle
            ui.ATSApp.animate_fan(app, app.gadget)
            self.assertAlmostEqual(app._fan_angle, first * 4)
            app.fan_status = None
            ui.ATSApp.animate_fan(app, app.gadget)
            angle = app._fan_angle
            ui.ATSApp.animate_fan(app, app.gadget)
            self.assertEqual(angle, app._fan_angle)

    def test_response_timestamp_is_completion_not_request_start(self):
        callbacks = []
        app = SimpleNamespace(fan_endpoint='http://example', _fan_busy=False,
                              _fan_generation=1, _fan_last_poll_at=0,
                              safe_after_from_worker=callbacks.append)
        class ImmediateThread:
            def __init__(self, target, daemon): self.target = target
            def start(self): self.target()
        def operation(client):
            ui.time.monotonic()  # request starts at 100, completes at 105
            return {'online': True}
        with patch.object(ui.threading, 'Thread', ImmediateThread), patch.object(ui.time, 'monotonic', side_effect=[100, 105]):
            ui.ATSApp.fan_request(app, operation)
        callbacks.pop()()
        self.assertEqual(app._fan_last_poll_at, 105)

    def test_poll_frequency_is_two_seconds_without_fast_error_retry(self):
        app = SimpleNamespace(fan_endpoint='http://example', _fan_busy=False, fan_status=None,
                              _fan_last_poll_at=0, update_fan_dialog=lambda: None,
                              schedule_after=lambda *args: None, poll_fan=lambda: None,
                              fan_request=lambda operation, done: done(False, 'timeout'))
        with patch.object(ui.time, 'monotonic', return_value=100):
            ui.ATSApp.poll_fan(app)
        self.assertEqual(app._fan_next_poll_at, 102)

if __name__ == '__main__':
    unittest.main()
