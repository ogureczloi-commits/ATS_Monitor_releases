import ast
import pathlib
import types
import unittest

SOURCE = pathlib.Path(__file__).with_name('ats_monitor_ui.py')


class Widget:
    def __init__(self, parent=None, **kwargs):
        self.parent = parent
        self.children = []
        if parent is not None and hasattr(parent, 'children'):
            parent.children.append(self)
        self.options = kwargs
        self.value = ''
        self.visible = False
    def pack(self, **kwargs): self.visible = True
    def pack_propagate(self, *args): pass
    def pack_forget(self): self.visible = False
    def configure(self, **kwargs): self.options.update(kwargs)
    def insert(self, index, value): self.value = str(value)
    def delete(self, *args): self.value = ''
    def bind(self, *args): pass
    def set(self, value): self.value = value
    def get(self): return self.value
    def winfo_exists(self): return True
    def winfo_manager(self): return 'pack' if self.visible else ''
    def title(self, *args): pass
    def geometry(self, *args): pass
    def minsize(self, *args): pass
    def lift(self): pass
    def transient(self, *args): pass
    def focus_set(self): pass
    def update_idletasks(self): pass
    def focus_force(self): pass


class FanDialogLayoutTests(unittest.TestCase):
    def test_advanced_section_collapses_without_losing_edits(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and any(isinstance(x, ast.FunctionDef) and x.name == 'open_fan_settings' for x in n.body))
        methods = [x for x in cls.body if isinstance(x, ast.FunctionDef) and x.name in ('open_fan_settings', 'update_fan_dialog')]
        module = ast.Module(body=[ast.ClassDef(name='Dialog', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
        ctk = types.SimpleNamespace(**{name: Widget for name in ('CTkToplevel', 'CTkScrollableFrame', 'CTkFrame', 'CTkLabel', 'CTkEntry', 'CTkButton', 'CTkSegmentedButton', 'CTkSlider')}, CTkFont=lambda **kw: None)
        env = {'ctk': ctk, 'COLOR_CARD': 'white', 'COLOR_CARD_ALT': 'white', 'COLOR_ACCENT_HOVER': 'blue', 'COLOR_WARNING': 'orange', 'FONT_FAMILY': 'Segoe UI', 'COLOR_BG': 'white', 'COLOR_SURFACE': 'white', 'COLOR_BORDER': 'grey', 'COLOR_ACCENT': 'blue', 'COLOR_MUTED': 'grey', 'COLOR_TEXT': 'black', 'status_settings': lambda s: {}}
        exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), env)
        app = env['Dialog']()
        app.fan_endpoint = ''
        app.fan_status = None
        app.open_fan_settings()
        def walk(widget):
            yield widget
            for child in widget.children: yield from walk(child)
        widgets = list(walk(app._fan_window))
        toggle = next((w for w in widgets if 'Дополнительные' in w.options.get('text', '') and 'command' in w.options), None)
        self.assertIsNotNone(toggle, 'advanced settings need a toggle')
        field = app._fan_fields['start_c']
        self.assertFalse(field.parent.parent.visible)
        toggle.options['command']()
        self.assertTrue(field.parent.parent.visible)
        field.insert(0, '25')
        toggle.options['command']()
        self.assertFalse(field.parent.parent.visible)
        toggle.options['command']()
        self.assertEqual(field.get(), '25')

if __name__ == '__main__': unittest.main()
