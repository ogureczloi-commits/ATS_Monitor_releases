"""HTTP client for the independent fan controller; speed is commanded %, not RPM."""
import json
import math
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def normalize_endpoint(value):
    value = value.strip().rstrip('/')
    if not value:
        return ''
    if '://' not in value:
        value = 'http://' + value
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Некорректный адрес вентилятора') from exc
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.path or parsed.query or parsed.fragment):
        raise ValueError('Укажите только http(s)://адрес:порт без пути и пароля')
    return value


def normalize_status(data):
    if not isinstance(data, dict):
        raise ValueError('Некорректный ответ вентилятора')
    online = data.get('online', True)
    if not isinstance(online, bool):
        raise ValueError('Некорректный статус связи')
    speed = data.get('output_speed')
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0 <= speed <= 100:
        raise ValueError('Некорректный заданный процент')
    if data.get('mode') not in ('auto', 'manual'):
        raise ValueError('Некорректный режим')
    return dict(data, online=online)


def status_settings(status):
    keys = ('start_c', 'stop_c', 'max_c', 'delta_c', 'alarm_c', 'min_power', 'max_power', 'room_rom', 'outdoor_rom')
    return {key: status[key] for key in keys if key in status}


def parse_settings_form(values):
    try:
        result = {k: float(values[k].strip().replace(',', '.')) for k in ('start_c', 'stop_c', 'max_c', 'delta_c', 'alarm_c')}
        result.update({k: int(values[k].strip()) for k in ('min_power', 'max_power')})
        result.update({k: values[k].strip() for k in ('room_rom', 'outdoor_rom')})
    except (ValueError, KeyError, AttributeError) as exc:
        raise ValueError('Проверьте числовые поля настроек') from exc
    if (not all(math.isfinite(result[k]) for k in ('start_c', 'stop_c', 'max_c', 'delta_c', 'alarm_c')) or
            not -55 <= result['stop_c'] < result['start_c'] < result['max_c'] <= 125 or
            not 0 <= result['delta_c'] <= 180 or not -55 <= result['alarm_c'] <= 125 or
            not 0 <= result['min_power'] <= result['max_power'] <= 100):
        raise ValueError('Проверьте диапазоны: стоп < старт < максимум; мощность 0–100%')
    return result


class FanClient:
    def __init__(self, endpoint, timeout=3):
        self.endpoint = normalize_endpoint(endpoint)
        if not self.endpoint:
            raise ValueError('Адрес вентилятора не задан')
        self.timeout = timeout

    def _request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        request = Request(self.endpoint + path, data=body, headers={'Content-Type':'application/json'} if body is not None else {})
        with urlopen(request, timeout=self.timeout) as response:
            result = response.read(65537)
        if len(result) > 65536:
            raise ValueError('Ответ вентилятора слишком велик')
        return json.loads(result.decode('utf-8')) if result else {}

    def status(self):
        return normalize_status(self._request('/api/status'))

    def control(self, mode, speed=None):
        if mode not in ('auto', 'manual'):
            raise ValueError('Режим должен быть auto или manual')
        payload = {'mode': mode}
        if mode == 'manual':
            if isinstance(speed, bool) or not isinstance(speed, int) or not 0 <= speed <= 100:
                raise ValueError('Процент должен быть целым числом от 0 до 100')
            payload['speed'] = speed
        return normalize_status(self._request('/api/control', payload))

    def settings(self, settings):
        if not isinstance(settings, dict):
            raise ValueError('Настройки должны быть JSON-объектом')
        return normalize_status(self._request('/api/settings', settings))
