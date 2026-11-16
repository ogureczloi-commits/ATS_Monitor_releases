import unittest,queue,threading,ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock,patch
import ats_monitor_ui as ui
import test_updater

class Release150Tests(unittest.TestCase):
    def test_version_and_assets(self):
        root=Path(ui.__file__).parent
        self.assertEqual(ui.APP_VERSION,'1.5.0')
        self.assertEqual((root/'VERSION').read_text().strip(),ui.APP_VERSION)
        self.assertTrue((root/'sounds/alert.wav').is_file())
        self.assertEqual(Path(ui.ESP_CONFIG).parent,Path(ui.USER_DATA_DIR))

    def test_failed_connection_does_not_disable_future_writes_and_busy_queues_offline(self):
        pending=[]
        class Thread:
            def __init__(self,target,daemon):pending.append(target)
            def start(self):pass
        app=SimpleNamespace(_closing=False,_db_log_busy=False,_db_log_queue=queue.Queue(),_db_log_lock=threading.Lock())
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
        with patch.object(ui.threading,'Thread',Thread),patch.object(ui,'get_db_conn',side_effect=[OSError('unavailable'),conn]),patch.object(ui,'log_esp_data') as log:
            ui.ATSApp.log_data_async(app,temp=24,hum=40,press=750,door=False,online=True)
            ui.ATSApp.log_data_async(app,temp=None,hum=None,press=None,door=None,online=False)
            self.assertEqual(len(pending),1)
            pending.pop()()
            self.assertFalse(app._db_log_busy)
            self.assertFalse(log.call_args.kwargs['online'])
            conn.close.assert_called_once()

class ProtectedSettingsTests(unittest.TestCase):
    def setUp(self):
        test_updater.UpdaterRegressionTests.setUp(self)
    def tearDown(self):
        test_updater.UpdaterRegressionTests.tearDown(self)
    def test_new_settings_not_overwritten_by_payload(self):
        install=self.root/'install';payload=self.root/'payload';install.mkdir();payload.mkdir()
        for name in ('ats_monitor_local_config.json','fan_endpoint.txt','esp_last_ip.txt'):
            (install/name).write_text('original')
            (payload/name).write_text('replacement')
        self.updater.copy_tree_contents(payload,install)
        for name in ('ats_monitor_local_config.json','fan_endpoint.txt','esp_last_ip.txt'):
            self.assertEqual((install/name).read_text(),'original')
if __name__=='__main__':unittest.main()
