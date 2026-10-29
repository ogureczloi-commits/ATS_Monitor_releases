import importlib.util
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
UPDATER_PATH = PROJECT_DIR / "updater.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("ats_updater", UPDATER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpdaterRegressionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ats_updater_test_"))
        self.updater = load_updater()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_wait_for_specific_pid_returns_after_child_exits(self):
        calls = []
        original_run = self.updater.subprocess.run

        def fake_run(*args, **kwargs):
            calls.append(args)
            class Result:
                stdout = f"PID 4242 running" if len(calls) == 1 else "INFO: No tasks are running"
            return Result()

        self.updater.subprocess.run = fake_run
        try:
            self.assertTrue(self.updater.wait_for_pid(4242, timeout=1))
        finally:
            self.updater.subprocess.run = original_run
        self.assertEqual(len(calls), 2)

    def test_copy_payload_replaces_application_and_preserves_local_secret(self):
        install = self.root / "install"
        payload = self.root / "payload"
        (install / "_internal").mkdir(parents=True)
        (payload / "_internal").mkdir(parents=True)
        (install / "ats_monitor_ui.exe").write_text("old", encoding="utf-8")
        (install / "_internal" / "runtime.txt").write_text("old-runtime", encoding="utf-8")
        (install / "openweather_api_key.txt").write_text("keep-me", encoding="utf-8")
        (payload / "ats_monitor_ui.exe").write_text("new", encoding="utf-8")
        (payload / "_internal" / "runtime.txt").write_text("new-runtime", encoding="utf-8")
        (payload / "updater.exe").write_text("new-updater", encoding="utf-8")

        self.updater.copy_tree_contents(payload, install)

        self.assertEqual((install / "ats_monitor_ui.exe").read_text(encoding="utf-8"), "new")
        self.assertEqual((install / "_internal" / "runtime.txt").read_text(encoding="utf-8"), "new-runtime")
        self.assertEqual((install / "updater.exe").read_text(encoding="utf-8"), "new-updater")
        self.assertEqual((install / "openweather_api_key.txt").read_text(encoding="utf-8"), "keep-me")

    def test_staged_updater_path_is_outside_application_directory(self):
        app_dir = self.root / "install"
        app_dir.mkdir()
        source = app_dir / "updater.exe"
        source.write_text("updater", encoding="utf-8")

        staged = Path(self.updater.stage_updater(str(source), "ATS Monitor"))

        self.assertTrue(staged.is_file())
        self.assertNotEqual(staged.parent.resolve(), app_dir.resolve())
        self.assertEqual(staged.read_text(encoding="utf-8"), "updater")


if __name__ == "__main__":
    unittest.main(verbosity=2)
