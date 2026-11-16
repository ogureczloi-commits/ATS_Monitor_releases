import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import ats_monitor_ui as app


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {'room_temp': 25, 'humidity': 50} if self.path == '/status' else None
        if self.path == '/api/status':
            payload = {'mode': 'manual', 'output_speed': 0, 'room_valid': True, 'outdoor_valid': True}
        self.send_response(200 if payload else 404)
        self.end_headers()
        self.wfile.write(json.dumps(payload or {}).encode())

    def log_message(self, *args):
        pass


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = '127.0.0.1:%s' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_probe_identifies_both_endpoint_contracts(self):
        self.assertEqual({kind for kind, address in app.probe_esp_devices(self.host)}, {'sensor', 'fan'})

    def test_probe_rejects_unrelated_json(self):
        with patch.object(app.requests, 'get') as sensor, patch.object(app.FanClient, 'status', return_value={'mode': 'manual', 'output_speed': 0}):
            sensor.return_value.status_code = 200
            sensor.return_value.json.return_value = {'mode': 'manual', 'output_speed': 0}
            self.assertEqual(app.probe_esp_devices('example.test'), [])

    def test_remembered_fan_is_a_scan_candidate(self):
        with patch.object(app, 'build_scan_candidates', return_value=['sensor-host']), \
             patch.object(app, 'probe_esp_devices', side_effect=lambda host, timeout: [('fan', host)] if host == 'fan-host:8080' else []):
            self.assertEqual(app.scan_for_devices(fan_endpoint='http://fan-host:8080'), [('fan', 'fan-host:8080')])

    def test_scan_does_not_stop_after_sensor(self):
        with patch.object(app, 'build_scan_candidates', return_value=['sensor-host', 'fan-host']), \
             patch.object(app, 'probe_esp_devices', side_effect=lambda host, timeout: [('sensor', host)] if host == 'sensor-host' else [('fan', host)]):
            self.assertEqual(set(app.scan_for_devices()), {('sensor', 'sensor-host'), ('fan', 'fan-host')})


if __name__ == '__main__':
    unittest.main()
