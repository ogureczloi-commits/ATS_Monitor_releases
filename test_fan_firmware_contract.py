import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from fan_controller import FanClient, normalize_status


def firmware_status(**overrides):
    data = dict(mode='manual', requested_speed=70, output_speed=40, armed=True,
                room_valid=True, outdoor_valid=True, room_c=25.5, outdoor_c=15.0,
                alarm=False, alarm_known=True, sensor_fault=False,
                room_rom='280000000000001E', outdoor_rom='280000000000002B',
                start_c=24.0, stop_c=23.0, max_c=27.0, delta_c=2.0, alarm_c=28.0, min_power=15, max_power=90,
                wifi_connected=True, ip='192.0.2.1', ssid_configured=True,
                ap_active=False, rpm=None, discovered_roms=[])
    data.update(overrides)
    return data

class FirmwareContractTests(unittest.TestCase):
    def test_status_and_fault(self):
        from fan_controller import status_settings
        status = normalize_status(firmware_status())
        self.assertEqual(status['output_speed'], 40)
        self.assertEqual(status['requested_speed'], 70)
        self.assertTrue(status['online'])
        fault = normalize_status(firmware_status(room_valid=False, room_c=None, alarm_known=False, sensor_fault=True, armed=False, output_speed=0))
        self.assertIsNone(fault['room_c'])
        self.assertTrue(fault['sensor_fault'])
        self.assertEqual(status_settings(status), {k: status[k] for k in ('start_c', 'stop_c', 'max_c', 'delta_c', 'alarm_c', 'min_power', 'max_power', 'room_rom', 'outdoor_rom')})
        for speed in (-1, 101, True, 'bad'):
            with self.assertRaises(ValueError): normalize_status(firmware_status(output_speed=speed))

    def test_http_contract(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                calls.append((self.command, self.path, None)); self.reply(firmware_status())
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append((self.command, self.path, payload))
                if self.path == '/api/control':
                    self.reply(firmware_status(mode=payload['mode'], requested_speed=payload.get('speed', 70), output_speed=40))
                else:
                    self.reply(firmware_status(**payload))
            def reply(self, data):
                body = json.dumps(data).encode(); self.send_response(200); self.end_headers(); self.wfile.write(body)
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = FanClient('http://127.0.0.1:%d' % server.server_port)
            self.assertEqual(client.status()['output_speed'], 40)
            self.assertEqual(client.control('manual', 80)['requested_speed'], 80)
            self.assertEqual(client.control('auto')['output_speed'], 40)
            self.assertEqual(client.settings({'start_c': 25})['start_c'], 25)
            self.assertEqual(calls, [('GET', '/api/status', None), ('POST', '/api/control', {'mode': 'manual', 'speed': 80}), ('POST', '/api/control', {'mode': 'auto'}), ('POST', '/api/settings', {'start_c': 25})])
            for mode, speed in [('manual', 1.5), ('manual', True), ('invalid', 1)]:
                with self.assertRaises(ValueError): client.control(mode, speed)
        finally:
            server.shutdown(); thread.join(); server.server_close()

if __name__ == '__main__': unittest.main()
