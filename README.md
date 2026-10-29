# ATS Monitor

> Windows-мониторинг помещения АТС: ESP/Wemos, температура, влажность,
> давление, дверь, RTSP-камера, история PostgreSQL и безопасные обновления.

[![Version](https://img.shields.io/badge/version-1.4.2-2563EB?style=flat-square)](CHANGELOG.md)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square)](#запуск-и-сборка)
[![Privacy](https://img.shields.io/badge/secrets-local_only-16A34A?style=flat-square)](#локальная-конфигурация)

## Возможности

| Контроль помещения | Работа оператора | Надёжность |
| --- | --- | --- |
| Температура, влажность, давление и дверь через ESP | Светлый компактный интерфейс и системный трей | Фоновая работа сети без блокировки UI |
| Графики последних измерений и история PostgreSQL | Поиск ESP в локальной сети и ручной ввод IP | Локальные настройки сохраняются при OTA-обновлении |
| RTSP-предпросмотр и отдельное полноэкранное окно | Сезонные предупреждения: вентиляция зимой и открытая дверь летом | Внешний updater заменяет onedir-сборку безопасно |
| Погода OpenWeatherMap для режимов оповещений | Открытие веб-интерфейса выбранной ESP | Резервная копия и откат при ошибке обновления |

## Структура актуальной версии

```text
ats_monitor_ui.py                    Основное приложение и интерфейс
ats_monitor_local_config.py          Чтение локальной конфигурации
ats_monitor_local_config.example.json Шаблон конфигурации без секретов
ats_monitor_ui.spec                  Сборка основного onedir-приложения
updater.py / updater.spec            Безопасный обновлятор
icons/ и sounds/                     Ресурсы интерфейса и оповещений
VERSION / CHANGELOG.md               Номер и история версии
test_updater.py                      Регрессионные тесты обновлятора
```

## Локальная конфигурация

Скопируйте шаблон и заполните его только на целевом ПК:

```bat
copy ats_monitor_local_config.example.json ats_monitor_local_config.json
```

В `ats_monitor_local_config.json` хранятся параметры PostgreSQL и RTSP-адреса
камер. Рядом с программой также локально хранятся:

- `openweather_api_key.txt` — ключ OpenWeatherMap;
- `esp_last_ip.txt` — последний найденный IP ESP;
- `ats_monitor_local_config.json` — подключение к БД и камеры.

Эти файлы исключены через `.gitignore`, не входят в репозиторий и не должны
добавляться в релизный ZIP.

## Запуск и сборка

Для работы с исходниками используется окружение Python 3.12:

```bat
venv312\Scripts\python.exe ats_monitor_ui.py
```

Проверка перед сборкой:

```bat
venv312\Scripts\python.exe -m py_compile ats_monitor_ui.py ats_monitor_local_config.py updater.py
venv312\Scripts\python.exe -m unittest -v test_updater.py
```

Сборка всегда выполняется в новую версионную папку. Не заменяйте действующую
установку до проверки собранного приложения.

## Обновления

Приложение проверяет GitHub Releases репозитория
`ogureczloi-commits/ATS_Monitor_releases`. Пакет обновления — полный ZIP
onedir-сборки с `updater.exe`; локальные `openweather_api_key.txt`,
`esp_last_ip.txt` и `ats_monitor_local_config.json` должны сохраняться.

Базовая проверенная версия OTA: `1.4.2`.

## Правила репозитория

Не добавляйте в Git:

- пароли, токены, ключи API, `.env` и локальную конфигурацию;
- EXE, ZIP, `build*`, `dist*`, виртуальные окружения и кэш Python;
- старые исходники, резервные копии, данные датчиков и пользовательские файлы.

История изменений: [CHANGELOG.md](CHANGELOG.md).
