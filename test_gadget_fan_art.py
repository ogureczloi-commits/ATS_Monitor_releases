import ast
import math
import pathlib
from types import SimpleNamespace
import unittest


class Canvas:
    def __init__(self):
        self.shapes = []
    def delete(self, *args):
        self.shapes.clear()
    def winfo_reqwidth(self):
        return 60
    def scale(self, *args):
        pass
    def __getattr__(self, name):
        if name.startswith('create_'):
            return lambda *args, **kwargs: self.shapes.append((name, args, kwargs))
        raise AttributeError(name)


class Widget:
    def winfo_exists(self):
        return True
    def configure(self, **kwargs):
        self.text = kwargs['text']


def animation_method():
    path = pathlib.Path(__file__).with_name('ats_monitor_ui.py')
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ATSApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'animate_fan')
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    scope = dict(math=math, time=SimpleNamespace(monotonic=lambda: 100), COLOR_BORDER='#d1d5db', COLOR_ACCENT='#2563eb', COLOR_MUTED='#64748b', COLOR_SURFACE='#ffffff')
    exec(compile(module, str(path), 'exec'), scope)
    return scope['animate_fan']


class FanArtTests(unittest.TestCase):
    def test_fan_blades_are_curved_and_motion_trails_only_when_running(self):
        animate = animation_method()
        class App:
            schedule_after = lambda self, *args: None
        app = App()
        app.gadget = Widget()
        app.fan_canvas = Canvas()
        app.fan_gadget_label = Widget()
        app._fan_angle = 0.0
        app._fan_animation_at = 99.9
        app.fan_status = {'output_speed': 0, 'mode': 'auto'}
        animate(app, app.gadget)
        stopped = list(app.fan_canvas.shapes)
        self.assertEqual(app._fan_angle, 0)
        self.assertGreaterEqual(sum(name == 'create_polygon' for name, *_ in stopped), 3)
        self.assertTrue(any(kwargs.get('smooth') for name, args, kwargs in stopped if name == 'create_polygon'))
        app.fan_status = {'output_speed': 80, 'mode': 'auto'}
        app._fan_animation_at = 99.9
        animate(app, app.gadget)
        running = list(app.fan_canvas.shapes)
        self.assertGreater(app._fan_angle, 0)
        self.assertGreater(sum(name == 'create_polygon' for name, *_ in running), sum(name == 'create_polygon' for name, *_ in stopped))
        angle = app._fan_angle
        app.fan_status = None
        animate(app, app.gadget)
        self.assertEqual(app._fan_angle, angle)
        self.assertIn('Нет связи', app.fan_gadget_label.text)
        self.assertEqual(sum(name == 'create_polygon' for name, *_ in app.fan_canvas.shapes), sum(name == 'create_polygon' for name, *_ in stopped))

    def test_rotation_is_faster_and_proportional(self):
        animate = animation_method()
        class App:
            schedule_after = lambda self, *args: None
        app = App()
        app.gadget = Widget()
        app.fan_canvas = Canvas()
        app.fan_gadget_label = Widget()
        angles = []
        for speed in (0, 50, 100):
            app._fan_angle = 0
            app._fan_animation_at = 99.9
            app.fan_status = {'output_speed': speed, 'mode': 'manual'}
            animate(app, app.gadget)
            angles.append(app._fan_angle)
        self.assertEqual(angles[0], 0)
        self.assertAlmostEqual(angles[2], 2 * angles[1])
        self.assertGreater(angles[2], 0.25)


if __name__ == '__main__':
    unittest.main()
