import unittest, threading, queue
from types import SimpleNamespace
from unittest.mock import Mock, patch
import ats_monitor_ui as ui

class ResilienceTests(unittest.TestCase):
    def test_invalid_sensor_payloads(self):
        for value in (None, 'broken', float('nan'), float('inf'), True):
            self.assertIsNone(ui.validated_esp({'room_temp':value,'humidity':40}))
        self.assertEqual(ui.validated_esp({'room_temp':'24.5','humidity':40})['room_temp'],24.5)

    def test_stale_or_invalid_sensor_does_not_stop_loop(self):
        for data, timestamp in (({'room_temp':None,'humidity':40},100),({'room_temp':24,'humidity':40},80)):
            app=SimpleNamespace(_esp_refresh_running=True,_fan_last_poll_at=0,_latest_esp=data,
                _latest_esp_at=timestamp,ESP_TIMEOUT=10,esp_online=False,_closing=False,
                room_label=Mock(),door_state_label=Mock(),status_frame=Mock(),status_label=Mock(),
                refresh_gadget=Mock(),update_label=Mock(),schedule_after=Mock(),loop=Mock())
            with patch.object(ui.time,'monotonic',return_value=100),patch.object(ui,'check_alerts'):
                ui.ATSApp.loop(app)
            app.schedule_after.assert_called_once()
            self.assertIn('OFFLINE',app.status_label.configure.call_args.kwargs['text'])

    def test_camera_frame_expires(self):
        reader=ui.CameraReader.__new__(ui.CameraReader)
        reader._lock=threading.Lock();reader._frame=Mock();reader._frame_at=10
        with patch.object(ui.time,'monotonic',return_value=16):self.assertIsNone(reader.latest_frame())
        reader._frame.copy.assert_not_called()

    def test_camera_switch_keeps_single_render_timer(self):
        app=SimpleNamespace(camera_reader=None,schedule_after=Mock(),update_camera=Mock())
        with patch.object(ui,'CameraReader') as reader:
            ui.ATSApp.start_camera(app,'first');ui.ATSApp.start_camera(app,'second')
            app.schedule_after.assert_called_once()
            reader.return_value.stop.assert_called_once()

    def test_db_failure_closes_connection_and_releases_busy(self):
        pending=[]
        class Thread:
            def __init__(self,target,daemon):pending.append(target)
            def start(self):pass
        app=SimpleNamespace(_db_log_busy=False,_closing=False, _db_log_queue=queue.Queue(), _db_log_lock=threading.Lock())
        conn=Mock();conn.__enter__=Mock(return_value=conn);conn.__exit__=Mock(return_value=False)
        conn.cursor.side_effect=RuntimeError('simulated SQL failure')
        with patch.object(ui.threading,'Thread',Thread),patch.object(ui,'get_db_conn',return_value=conn):
            ui.ATSApp.log_data_async(app,temp=20,hum=40,press=750,door=False,online=True)
            self.assertTrue(app._db_log_busy);conn.cursor.assert_not_called()
            pending.pop()()
        conn.close.assert_called_once();self.assertFalse(app._db_log_busy)
        self.assertEqual(conn.__exit__.call_args.args[0],RuntimeError)

if __name__=='__main__':unittest.main()
