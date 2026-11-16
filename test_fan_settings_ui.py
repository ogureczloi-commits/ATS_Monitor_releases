import unittest
from fan_controller import parse_settings_form


class SettingsFormTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(start_c='24', stop_c='23', max_c='27', delta_c='2', alarm_c='28', min_power='15', max_power='90', room_rom='', outdoor_rom='')

    def test_parse_valid_values(self):
        result = parse_settings_form(self.values)
        self.assertEqual(result['min_power'], 15)
        self.assertEqual(result['start_c'], 24.0)

    def test_invalid_values_rejected(self):
        for key, value in [('start_c', '23'), ('min_power', '91'), ('max_power', '100.5'), ('delta_c', 'nan'), ('alarm_c', '126')]:
            values = dict(self.values, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_settings_form(values)

if __name__ == '__main__':
    unittest.main()
