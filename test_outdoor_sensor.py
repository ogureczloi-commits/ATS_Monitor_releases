import unittest
from unittest.mock import patch
import ats_monitor_ui as ui


class OutdoorSensorTests(unittest.TestCase):
    def test_fan_dialog_status_is_compact_and_formats_temperatures(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        app = SimpleNamespace(_fan_window=Mock(), _fan_connection_label=Mock(),
            _fan_alarm_label=Mock(), _fan_output_label=Mock(), _fan_current_mode_label=Mock(),
            _fan_temperatures_label=Mock(), _fan_save_button=Mock(), _fan_mode_initialized=True,
            _fan_fields={}, _fan_settings_dirty=True,
            fan_status={'room_c': 23, 'room_valid': True, 'outdoor_c': -5.25,
                        'outdoor_valid': True, 'alarm_known': True, 'alarm': False,
                        'output_speed': 40, 'mode': 'auto'})
        ui.ATSApp.update_fan_dialog(app)
        text = app._fan_temperatures_label.configure.call_args.kwargs['text']
        self.assertIn('23.0 °C', text)
        self.assertIn('-5.2 °C', text)
        self.assertEqual(app._fan_output_label.configure.call_args.kwargs['text'], '40%')
        self.assertIn('Аварии нет', app._fan_alarm_label.configure.call_args.kwargs['text'])
        app.fan_status = None
        ui.ATSApp.update_fan_dialog(app)
        self.assertIn('Нет связи', app._fan_connection_label.configure.call_args.kwargs['text'])

    def test_loop_clears_outdoor_and_alert_when_fan_disconnects(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        app = SimpleNamespace(_esp_refresh_running=True, _fan_last_poll_at=100, _latest_esp_at=100, ESP_TIMEOUT=20,
            fan_status={'online': True, 'outdoor_valid': True, 'outdoor_c': -5},
            _latest_esp={'room_temp': 25, 'humidity': 40, 'pressure': 750, 'door': False},
            esp_online=True, db_conn=None, db_cursor=None, _closing=True,
            room_label=Mock(), status_label=Mock(), update_label=Mock(),
            update_door_presentation=Mock(), refresh_gadget=Mock(), draw_door=Mock(), update_graph=Mock())
        with patch.object(ui.time, 'monotonic', return_value=101), patch.object(ui, 'check_alerts') as alerts:
            ui.ATSApp.loop(app)
            self.assertEqual(ui.outside_temp, -5)
            self.assertIn('Улица: -5.0 °C', app.room_label.configure.call_args.kwargs['text'])
            self.assertEqual(ui.season_mode, 'ЗИМА')
            app.fan_status = None
            ui.ATSApp.loop(app)
            self.assertIsNone(ui.outside_temp)
            self.assertEqual(ui.season_mode, '—')
            self.assertIsNone(alerts.call_args.kwargs['outside_temp'])
            self.assertIn('Улица: --', app.room_label.configure.call_args.kwargs['text'])
            app.fan_status = {'online': True, 'outdoor_valid': True, 'outdoor_c': 20}
            with patch.object(ui.time, 'monotonic', return_value=113):
                ui.ATSApp.loop(app)
            self.assertIsNone(ui.outside_temp)
            self.assertEqual(ui.season_mode, '—')

    def test_outdoor_requires_online_valid_finite_sensor(self):
        self.assertEqual(ui.outdoor_from_fan({'online': True, 'outdoor_valid': True, 'outdoor_c': -4.5}), -4.5)
        for status in (None, {'online': False, 'outdoor_valid': True, 'outdoor_c': 4},
                       {'online': True, 'outdoor_valid': False, 'outdoor_c': 4},
                       {'online': True, 'outdoor_valid': True, 'outdoor_c': float('nan')},
                       {'online': True, 'outdoor_valid': True, 'outdoor_c': True},
                       {'online': True, 'outdoor_valid': True, 'outdoor_c': 126}):
            self.assertIsNone(ui.outdoor_from_fan(status))

    def test_season_unknown_on_disconnect_and_deadband(self):
        self.assertEqual(ui.detect_season(None, 'ЛЕТО'), '—')
        self.assertEqual(ui.detect_season(12, '—'), '—')
        self.assertEqual(ui.detect_season(12, 'ЗИМА'), 'ЗИМА')
        self.assertEqual(ui.detect_season(16, 'ЗИМА'), 'ЛЕТО')
        self.assertEqual(ui.detect_season(9, 'ЛЕТО'), 'ЗИМА')

    def test_alerts_clear_on_unknown_or_changed_conditions(self):
        class Alerts:
            def __init__(self): self.active = set()
            def show(self, key, *args): self.active.add(key)
            def clear(self, key): self.active.discard(key)
        alerts = Alerts()
        with patch.object(ui, 'alert_manager', alerts):
            ui.check_alerts(24, -2, False, 'ЗИМА')
            self.assertEqual(alerts.active, {'winter_vent'})
            ui.check_alerts(24, None, False, '—')
            self.assertEqual(alerts.active, set())
            ui.check_alerts(24, 20, True, 'ЛЕТО')
            self.assertEqual(alerts.active, {'door_summer'})
            ui.check_alerts(None, None, None, '—')
            self.assertEqual(alerts.active, set())
            ui.check_alerts(24, 20, True, 'ЛЕТО')
            ui.check_alerts(24, -2, False, 'ЗИМА')
            self.assertEqual(alerts.active, {'winter_vent'})


if __name__ == '__main__': unittest.main()
