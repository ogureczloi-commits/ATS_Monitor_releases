import unittest
from fan_controller import normalize_endpoint, normalize_status, FanClient

class FanTests(unittest.TestCase):
    def test_endpoint_validation(self):
        self.assertEqual(normalize_endpoint(' 192.168.1.12/ '), 'http://192.168.1.12')
        with self.assertRaises(ValueError):
            normalize_endpoint('http://user:pass@host')

    def test_status_requires_valid_commanded_percent(self):
        self.assertEqual(normalize_status({'mode':'auto','output_speed':42})['output_speed'], 42)
        for speed in (-1, 101, True, 'bad'):
            with self.assertRaises(ValueError):
                normalize_status({'mode':'auto','output_speed':speed})
        self.assertFalse(normalize_status({'online':False, 'mode':'auto','output_speed':0})['online'])

    def test_http_contract(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading, json
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                calls.append((self.command,self.path,None)); self.reply({'mode':'manual','output_speed':30})
            def do_POST(self):
                calls.append((self.command,self.path,json.loads(self.rfile.read(int(self.headers['Content-Length']))))); self.reply({'mode':'manual','output_speed':30})
            def reply(self, data):
                body=json.dumps(data).encode(); self.send_response(200); self.end_headers(); self.wfile.write(body)
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            client=FanClient('http://127.0.0.1:%d' % server.server_port)
            self.assertEqual(client.status()['output_speed'],30)
            client.control('manual',40); client.settings({'start_c':24})
            self.assertEqual(calls,[('GET','/api/status',None),('POST','/api/control',{'mode':'manual','speed':40}),('POST','/api/settings',{'start_c':24})])
        finally:
            server.shutdown(); thread.join(); server.server_close()

if __name__ == '__main__': unittest.main()
