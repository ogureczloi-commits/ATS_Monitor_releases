# ATS Monitor

Актуальный исходный код Windows-программы мониторинга помещения АТС.

## Актуальные файлы

- `ats_monitor_ui.py` — интерфейс и рабочая логика приложения.
- `ats_monitor_local_config.py` — загрузка локальной конфигурации.
- `ats_monitor_ui.spec` — сборка основного приложения.
- `updater.py` и `updater.spec` — OTA-обновление.
- `test_updater.py` — проверка обновлятора.
- `icons/`, `sounds/`, `VERSION`, `CHANGELOG.md` — ресурсы и история версии.

## Локальная конфигурация

Скопируйте `ats_monitor_local_config.example.json` в
`ats_monitor_local_config.json` рядом с приложением и заполните параметры
подключения к БД и RTSP-адреса камер.

Этот файл, ключ OpenWeatherMap и сохранённый IP ESP не хранятся в Git.
Они остаются только на локальном ПК или рядом с установленной программой.

## Проверка

```bat
venv312\Scripts\python.exe -m py_compile ats_monitor_ui.py ats_monitor_local_config.py updater.py
venv312\Scripts\python.exe -m unittest -v test_updater.py
```

## Правила репозитория

Не добавляйте в Git пароли, токены, ключи API, файлы локальной конфигурации,
готовые EXE/ZIP, каталоги сборок или старые версии приложения.
