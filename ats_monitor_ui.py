import customtkinter as ctk
import requests
import time
import os
import threading
import pystray
from pystray import MenuItem as item
import winsound
import socket
import ipaddress
import sys
import subprocess
import tempfile
import zipfile
import shutil
import webbrowser
import concurrent.futures
import queue
import cv2
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
from tkinter import ttk
from psycopg2.extras import RealDictCursor
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg





from PIL import Image, ImageTk
from collections import deque

import matplotlib.pyplot as plt
from ats_monitor_local_config import load_local_config, config_value

if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    USER_DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    USER_DATA_DIR = BASE_DIR


# ================= НАСТРОЙКИ =================

APP_NAME = "ATS Monitor"
APP_VERSION = "1.4.2"
UPDATE_REPO = "ogureczloi-commits/ATS_Monitor_releases"
UPDATE_API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_CHECK_INTERVAL = 24 * 60 * 60

ESP_CONFIG = os.path.join(BASE_DIR, "esp_last_ip.txt")
ESP_URL = None
ESP_IP = None
REFRESH_SEC = 5
ESP_HOSTNAMES = ("ats-room.local", "ats_room.local")
ESP_SCAN_TIMEOUT = 0.35
ESP_SCAN_WORKERS = 64
ESP_SCAN_FAST_IPS = ("192.168.88.30",)

LOCAL_CONFIG = load_local_config(USER_DATA_DIR)

DB_HOST = config_value(LOCAL_CONFIG, "db_host")
DB_NAME = config_value(LOCAL_CONFIG, "db_name")
DB_USER = config_value(LOCAL_CONFIG, "db_user")
DB_PASS = config_value(LOCAL_CONFIG, "db_password")
DB_PORT = LOCAL_CONFIG.get("db_port", 5432)
if not isinstance(DB_PORT, int):
    DB_PORT = 5432


# ================= КАМЕРА (RTSP) =================
CAMERAS = {
    "— не выбрано —": "",
    "АТС (внешний)": config_value(LOCAL_CONFIG.get("cameras", {}), "АТС (внешний)"),
    "АТС (локально)": config_value(LOCAL_CONFIG.get("cameras", {}), "АТС (локально)"),
}

CURRENT_RTSP = ""  # сюда будет записываться выбранный/введённый RTSP


# OpenWeatherMap работает из РФ. Ключ хранится локально рядом с EXE,
# чтобы не попадать в исходный код, репозиторий и архивы обновлений.
WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"
WEATHER_API_KEY_FILE = os.path.join(USER_DATA_DIR, "openweather_api_key.txt")
WEATHER_PARAMS = {
    "lat": 55.75,
    "lon": 37.62,
    "units": "metric",
    "lang": "ru",
}


def load_weather_api_key():
    try:
        with open(WEATHER_API_KEY_FILE, "r", encoding="utf-8") as key_file:
            return key_file.read().strip()
    except OSError:
        return ""

MAX_POINTS = 60
temp_history = deque(maxlen=MAX_POINTS)
hum_history  = deque(maxlen=MAX_POINTS)

# ================= ДАННЫЕ =================

last_weather_update = 0
outside_temp = None
season_mode = "—"

# ================= ESP =================
def get_local_subnet():
    """Определяем локальную /24 сеть без внешних DNS-запросов."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect не отправляет пакет, но позволяет узнать выбранный локальный IP.
        s.connect(("192.168.88.1", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()

    return ipaddress.IPv4Network(ip + "/24", strict=False)


def is_esp_status(data):
    return isinstance(data, dict) and "room_temp" in data and "humidity" in data


def probe_esp_ip(ip, timeout=ESP_SCAN_TIMEOUT):
    url = f"http://{ip}/status"
    try:
        r = requests.get(url, timeout=timeout, headers={"Connection": "close"})
        if r.status_code == 200:
            data = r.json()
            if is_esp_status(data):
                return str(ip), data
    except Exception:
        pass
    return None


def resolve_hostname(name):
    try:
        return socket.gethostbyname(name)
    except Exception:
        return None


def build_scan_candidates():
    """Сначала проверяем самые вероятные адреса, потом всю подсеть."""
    candidates = []

    if ESP_IP:
        candidates.append(ESP_IP)

    for host in ESP_HOSTNAMES:
        ip = resolve_hostname(host)
        if ip:
            candidates.append(ip)

    candidates.extend(ESP_SCAN_FAST_IPS)

    try:
        candidates.extend(str(ip) for ip in get_local_subnet().hosts())
    except Exception:
        pass

    # Убираем дубли, сохраняя порядок.
    return list(dict.fromkeys(candidates))


def scan_for_esp(progress_cb=None, stop_event=None):
    """
    Быстрый параллельный поиск ESP.
    Раньше 254 адреса проверялись последовательно, поэтому окно долго висело.
    """
    found = []
    seen = set()
    candidates = build_scan_candidates()
    total = len(candidates)

    # Быстрая фаза: последний IP, mDNS и известный IP. Часто занимает доли секунды.
    fast_count = min(len(candidates), 8)
    for idx, ip in enumerate(candidates[:fast_count], start=1):
        if stop_event and stop_event.is_set():
            return found
        result = probe_esp_ip(ip, timeout=0.45)
        if progress_cb:
            progress_cb(idx, total, ip, bool(result))
        if result:
            found_ip, _ = result
            if found_ip not in seen:
                found.append(found_ip)
                seen.add(found_ip)
                return found

    checked = fast_count
    rest = candidates[fast_count:]
    if not rest:
        return found

    with concurrent.futures.ThreadPoolExecutor(max_workers=ESP_SCAN_WORKERS) as executor:
        future_to_ip = {executor.submit(probe_esp_ip, ip): ip for ip in rest}
        for future in concurrent.futures.as_completed(future_to_ip):
            ip = future_to_ip[future]
            checked += 1

            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                return found

            result = None
            try:
                result = future.result()
            except Exception:
                result = None

            if progress_cb:
                progress_cb(checked, total, ip, bool(result))

            if result:
                found_ip, _ = result
                if found_ip not in seen:
                    found.append(found_ip)
                    seen.add(found_ip)
                    # Для этой программы нужна одна ESP — нашли и прекращаем сканирование.
                    executor.shutdown(wait=False, cancel_futures=True)
                    return found

    return found


def set_esp_ip(ip):
    global ESP_URL, ESP_IP
    ESP_IP = ip.strip() if ip else None
    ESP_URL = f"http://{ESP_IP}/status" if ESP_IP else None


def get_esp_data():
    if not ESP_URL:
        return None
    try:
        r = requests.get(ESP_URL, timeout=0.8, headers={"Connection": "close"})
        if r.status_code == 200:
            data = r.json()
            if is_esp_status(data):
                return data
    except Exception:
        pass
    return None


class CameraReader:
    """Читает RTSP в отдельном потоке, чтобы OpenCV не блокировал интерфейс."""
    def __init__(self, rtsp_url):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._frame = None
        self._opened = False
        self._thread = threading.Thread(target=self._run, args=(rtsp_url,), daemon=True)
        self._thread.start()

    def _run(self, rtsp_url):
        cap = cv2.VideoCapture(rtsp_url)
        with self._lock:
            self._opened = cap.isOpened()
        try:
            while not self._stop.is_set() and cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    with self._lock:
                        self._frame = frame
                else:
                    self._stop.wait(0.08)
        finally:
            cap.release()
            with self._lock:
                self._opened = False

    def latest_frame(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def is_opened(self):
        with self._lock:
            return self._opened

    def stop(self):
        self._stop.set()


# ================= ПОГОДА =================

def get_outside_temp():
    api_key = load_weather_api_key()
    if not api_key:
        return None
    try:
        params = {**WEATHER_PARAMS, "appid": api_key}
        response = requests.get(WEATHER_URL, params=params, timeout=8)
        response.raise_for_status()
        return response.json()["main"]["temp"]
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return None

def detect_season(out_temp, prev):
    if out_temp is None:
        return prev
    if out_temp < 10:
        return "ЗИМА"
    if out_temp > 15:
        return "ЛЕТО"
    return prev


def check_alerts(room_temp, outside_temp, door_open, season):
    global door_open_since

    if season == "ЗИМА" and outside_temp is not None:
        if outside_temp < 0 and room_temp > 23 and not door_open:
            alert_manager.show(
                "winter_vent",
                "⚠️ Требуется проветривание",
                "Температура в помещении АТС растёт при отрицательной температуре на улице.\n\nОткройте дверь для проветривания."
            )
        else:
            alert_manager.clear("winter_vent")


    # ☀️ ЛЕТО — дверь открыта долго
    if season == "ЛЕТО":
        if door_open:
            alert_manager.show(
                "door_summer",
                "🚪 Дверь открыта",
                "Дверь открыта при включённом кондиционере.\n\nЗакройте дверь для сохранения охлаждения."
            )
        else:
            alert_manager.clear("door_summer")


def tray_show(icon, item):
    if app:
        app.safe_after_from_worker(lambda: (app.deiconify(), app.lift()))

def tray_quit(icon, item):
    icon.stop()
    if app:
        app.safe_after_from_worker(app.shutdown)

def get_db_conn():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )

def log_esp_data(cursor, temp, hum, press, door, online, esp_id="ATS_1"):
    cursor.execute("""
        INSERT INTO esp_log (
            esp_id,
            temperature,
            humidity,
            pressure,
            door_open,
            esp_online
        )
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        esp_id,
        temp,
        hum,
        press,
        door,
        online
    ))

# ================= UI =================
# Светлая компактная тема в стиле Phone Book.
COLOR_BG = "#F4F6FB"
COLOR_SURFACE = "#FFFFFF"
COLOR_CARD = "#FFFFFF"
COLOR_CARD_ALT = "#EEF3FB"
COLOR_BORDER = "#D8E1EF"
COLOR_TEXT = "#172033"
COLOR_MUTED = "#63708A"
COLOR_ACCENT = "#2563EB"
COLOR_ACCENT_HOVER = "#1D4ED8"
COLOR_SUCCESS = "#16A34A"
COLOR_DANGER = "#DC2626"
COLOR_WARNING = "#D97706"
FONT_FAMILY = "Segoe UI"
# Большой, но фиксированный предпросмотр: не сдвигает интерфейс при появлении кадра.
CAMERA_PREVIEW_SIZE = (420, 300)
CAMERA_MAX_RENDER_SIZE = CAMERA_PREVIEW_SIZE

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

class ATSApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title(f"{APP_NAME} v{APP_VERSION} · ЦТОП")
        self.geometry("940x620")
        self.minsize(820, 520)
        self.resizable(True, True)
        self.configure(fg_color=COLOR_BG)
        self.gadget = None
        self._last_door_open = None
        self._closing = False
        self._after_ids = set()
        self._ui_queue = queue.Queue()
        # БД подключается после отрисовки окна, чтобы недоступная сеть не подвешивала запуск.
        self.db_conn = None
        self.db_cursor = None
        self.db_connecting = False
        self.db_lock = threading.Lock()
        self.last_logged = 0
        self.LOG_INTERVAL = 30  # писать раз в 30 сек


        # ===== ESP connection state =====
        self.esp_online = False
        self.last_esp_seen = 0
        self.ESP_TIMEOUT = 10  # секунд без связи = OFFLINE

        # ===== Компактная шапка =====
        header = ctk.CTkFrame(self, fg_color=COLOR_SURFACE, corner_radius=0, height=58)
        header.pack(fill="x", padx=0, pady=0)
        header.pack_propagate(False)
        ctk.CTkLabel(
            header, text="АТС · Мониторинг",
            font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"), text_color=COLOR_TEXT,
        ).pack(side="left", padx=(18, 6), pady=12)
        ctk.CTkLabel(
            header, text=f"v{APP_VERSION}", font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            text_color=COLOR_MUTED,
        ).pack(side="left", pady=18)

        top_buttons = ctk.CTkFrame(header, fg_color="transparent")
        top_buttons.pack(side="right", padx=12, pady=10)
        button_options = dict(height=30, corner_radius=8, font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        ctk.CTkButton(top_buttons, text="ESP", command=self.open_esp_selector, width=58,
                      fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT, **button_options).pack(side="left", padx=2)
        ctk.CTkButton(top_buttons, text="Камера", command=self.open_camera_settings, width=70,
                      fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT, **button_options).pack(side="left", padx=2)
        self.history_btn = ctk.CTkButton(top_buttons, text="История", command=self.open_history_window, width=72,
                                         fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT, **button_options)
        self.history_btn.pack(side="left", padx=2)
        self.check_updates_btn = ctk.CTkButton(top_buttons, text="Обновить", command=lambda: self.check_for_updates(manual=True), width=78,
                                               fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT, **button_options)
        self.check_updates_btn.pack(side="left", padx=2)
        self.open_web_btn = ctk.CTkButton(top_buttons, text="ESP web", command=self.open_esp_web, width=75,
                                          fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, **button_options)
        self.open_web_btn.pack(side="left", padx=2)
        self.widget_btn = ctk.CTkButton(top_buttons, text="Гаджет", command=self.open_gadget, width=70,
                                        fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT, **button_options)
        self.widget_btn.pack(side="left", padx=(5, 0))

        # ===== Контейнер =====
        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.pack(fill="both", expand=True, padx=12, pady=10)
        # Полезное занимает большую часть компактного окна.
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_columnconfigure(1, weight=1)
        self.main.grid_rowconfigure(0, weight=0)
        self.main.grid_rowconfigure(1, weight=1)
        self.main.grid_rowconfigure(2, weight=0)



        # ===== Помещение =====
        self.room_frame = self.create_card("Показания помещения")
        self.room_frame.grid(row=0, column=0, padx=(0, 8), pady=(0, 8), sticky="nsew")
        self.room_label = ctk.CTkLabel(
            self.room_frame,
            text="Температура: --.- °C     Улица: --.- °C\nВлажность: -- %     Давление: ---- мм",
            justify="left", text_color=COLOR_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=16),
        )
        self.room_label.pack(padx=18, pady=(8, 16), anchor="w")

        # ===== Дверь и статус =====
        self.status_frame = self.create_card("Контроль доступа")
        self.status_frame.grid(row=0, column=1, padx=(8, 0), pady=(0, 8), sticky="nsew")
        status_body = ctk.CTkFrame(self.status_frame, fg_color="transparent")
        status_body.pack(fill="both", expand=True, padx=18, pady=(8, 14))
        self.door_state_label = ctk.CTkLabel(
            status_body, text="ДВЕРЬ: —", anchor="w", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=20, weight="bold"),
        )
        self.door_state_label.pack(side="left", fill="x", expand=True)
        self.status_label = ctk.CTkLabel(
            status_body, text="ESP: --\nРежим: —", justify="left", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
        )
        self.status_label.pack(side="right", padx=(8, 0))

        icons_dir = os.path.join(BASE_DIR, "icons")
        self.door_img_closed = ImageTk.PhotoImage(Image.open(os.path.join(icons_dir, "door_closed.png")).resize((54, 82)))
        self.door_img_open = ImageTk.PhotoImage(Image.open(os.path.join(icons_dir, "door_open.png")).resize((54, 82)))
        self.door_canvas = ctk.CTkCanvas(status_body, width=54, height=82, bg=COLOR_SURFACE, highlightthickness=0)
        self.door_canvas.pack(side="right", padx=(12, 0))
        self.door_icon = self.door_canvas.create_image(27, 41, image=self.door_img_closed)

        # ===== График =====
        self.graph_frame = ctk.CTkFrame(self.main, fg_color=COLOR_CARD, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        self.graph_frame.grid(row=1, column=0, padx=(0, 8), pady=(8, 0), sticky="nsew")

        fig, self.ax = plt.subplots(figsize=(5.2, 2.5), dpi=100)
        fig.patch.set_facecolor(COLOR_CARD)
        self.ax.set_facecolor(COLOR_CARD_ALT)
        self.ax.tick_params(colors=COLOR_MUTED)
        for spine in self.ax.spines.values():
            spine.set_color(COLOR_BORDER)
        self.temp_line, = self.ax.plot([], [], label="Температура", color="#2563EB", linewidth=2)
        self.hum_line, = self.ax.plot([], [], label="Влажность", color="#D97706", linewidth=2)
        self.ax.legend(facecolor=COLOR_CARD, labelcolor=COLOR_TEXT, edgecolor=COLOR_BORDER)
        self.canvas = FigureCanvasTkAgg(fig, master=self.graph_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)
        self.canvas.get_tk_widget().configure(bg=COLOR_CARD, highlightthickness=0)
        
        # ===== Камера =====
        self.camera_frame = ctk.CTkFrame(
            self.main, width=444, height=379, fg_color=COLOR_CARD,
            corner_radius=14, border_width=1, border_color=COLOR_BORDER,
        )
        self.camera_frame.grid(row=1, column=1, padx=(6, 0), pady=(6, 0), sticky="n")
        # Размер области фиксирован до появления первого кадра и после него.
        self.camera_frame.grid_propagate(False)
        self.camera_frame.pack_propagate(False)
        ctk.CTkLabel(
            self.camera_frame, text="Камера", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(10, 4))
        self.cam_canvas = ctk.CTkLabel(
            self.camera_frame, width=CAMERA_PREVIEW_SIZE[0], height=CAMERA_PREVIEW_SIZE[1], text="Камера не подключена",
            text_color=COLOR_MUTED, fg_color="#0F172A", corner_radius=8,
        )
        self.cam_canvas.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.open_cam_btn = ctk.CTkButton(
            self.camera_frame, text="Открыть", width=92, height=28, command=self.open_camera_window,
            fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT,
        )
        self.open_cam_btn.pack(pady=(0, 9))

        self.cap = None
        self.camera_reader = None
        self._esp_refresh_running = False
        self._latest_esp = None
        self._latest_esp_at = 0.0
        self._weather_refresh_running = False

        # ===== Мелкий статус обновления =====
        self.update_label = ctk.CTkLabel(
            self.main,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="#888888"
        )
        self.update_label.grid(row=2, column=1, sticky="e", padx=20)

        self.schedule_after(100, self.drain_ui_queue)
        self.schedule_after(300, self.connect_database_async)
        self.schedule_after(1000, self.loop)
        self.schedule_after(3000, lambda: self.check_for_updates(manual=False))
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

    def connect_database_async(self):
        """Подключает журнал БД вне UI-потока; мониторинг остаётся доступным без БД."""
        if self._closing or self.db_conn or self.db_connecting:
            return
        self.db_connecting = True

        def worker():
            try:
                conn = get_db_conn()
                cursor = conn.cursor()
            except Exception as exc:
                print("DB connection error:", exc)
                conn = cursor = None

            def apply_connection():
                self.db_connecting = False
                if conn is not None:
                    self.db_conn, self.db_cursor = conn, cursor
            self.safe_after_from_worker(apply_connection)

        threading.Thread(target=worker, daemon=True).start()

    def parse_version(self, version):
        version = str(version).strip().lstrip('vV')
        parts = []
        for chunk in version.split('.'):
            digits = ''.join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits or 0))
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])

    def check_for_updates(self, manual=False):
        if getattr(self, '_update_check_running', False):
            return
        self._update_check_running = True
        if manual:
            self.update_label.configure(text="Проверяю обновления...")
            self.check_updates_btn.configure(state="disabled")

        def worker():
            try:
                release = requests.get(UPDATE_API_URL, timeout=10, headers={"Accept": "application/vnd.github+json"}).json()
                tag = str(release.get('tag_name') or release.get('name') or '').strip()
                if not tag:
                    raise RuntimeError('В репозитории пока нет GitHub Releases')
                latest = tag.lstrip('vV')
                if self.parse_version(latest) <= self.parse_version(APP_VERSION):
                    self.safe_after_from_worker(lambda: self._finish_update_check(f"Версия актуальна: v{APP_VERSION}", manual))
                    return

                assets = release.get('assets') or []
                asset = None
                for item in assets:
                    name = (item.get('name') or '').lower()
                    if name.endswith('.zip') and 'ats' in name:
                        asset = item
                        break
                if asset is None:
                    for item in assets:
                        if (item.get('name') or '').lower().endswith('.zip'):
                            asset = item
                            break
                if asset is None:
                    raise RuntimeError(f"В релизе {tag} нет ZIP-архива")

                info = {
                    'tag': tag,
                    'version': latest,
                    'name': asset.get('name') or 'update.zip',
                    'url': asset.get('browser_download_url'),
                    'notes': release.get('body') or '',
                }
                self.safe_after_from_worker(lambda: self.show_update_window(info))
            except Exception as e:
                msg = f"Ошибка проверки обновлений: {e}" if manual else ""
                self.safe_after_from_worker(lambda: self._finish_update_check(msg, manual))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_update_check(self, message, manual=False):
        self._update_check_running = False
        if message:
            self.update_label.configure(text=message)
        if manual:
            self.check_updates_btn.configure(state="normal")

    def show_update_window(self, info):
        self._finish_update_check(f"Доступно обновление {info['tag']}", manual=True)
        win = ctk.CTkToplevel(self)
        win.title("Обновление ATS Monitor")
        win.geometry("520x340")
        win.resizable(False, False)
        win.attributes("-topmost", True)

        ctk.CTkLabel(win, text=f"Доступно обновление {info['tag']}", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(18, 8))
        ctk.CTkLabel(win, text=f"Текущая версия: v{APP_VERSION}\nНовая версия: {info['tag']}").pack(pady=(0, 8))
        notes = ctk.CTkTextbox(win, width=470, height=130)
        notes.pack(padx=20, pady=(0, 12))
        notes.insert("1.0", info.get('notes') or "Описание релиза пустое.")
        notes.configure(state="disabled")

        status = ctk.CTkLabel(win, text="", text_color="#cccccc")
        status.pack(pady=(0, 8))
        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack()

        def install():
            status.configure(text="Скачиваю и готовлю обновление...")
            for child in btns.winfo_children():
                child.configure(state="disabled")
            threading.Thread(target=lambda: self.download_and_start_update(info, status), daemon=True).start()

        ctk.CTkButton(btns, text="Скачать и установить", width=170, command=install).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="Позже", width=100, command=win.destroy).pack(side="left", padx=8)

    def download_and_start_update(self, info, status_label):
        try:
            if not getattr(sys, 'frozen', False):
                raise RuntimeError('Обновление доступно только для собранной EXE-версии')
            app_dir = os.path.dirname(sys.executable)
            updater = os.path.join(app_dir, 'updater.exe')
            if not os.path.exists(updater):
                raise RuntimeError('updater.exe не найден рядом с программой')

            temp_dir = tempfile.mkdtemp(prefix='ats_monitor_update_')
            zip_path = os.path.join(temp_dir, info['name'])
            with requests.get(info['url'], stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(zip_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)

            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.testzip()

            # The updater must not run from app_dir: it needs to replace updater.exe too.
            staged_dir = os.path.join(tempfile.gettempdir(), "ATS_Monitor_updater")
            os.makedirs(staged_dir, exist_ok=True)
            staged_updater = os.path.join(staged_dir, f"updater_{os.getpid()}_{int(time.time() * 1000)}.exe")
            shutil.copy2(updater, staged_updater)
            subprocess.Popen([
                staged_updater,
                '--app-dir', app_dir,
                '--zip', zip_path,
                '--exe', os.path.basename(sys.executable),
                '--wait-pid', str(os.getpid()),
            ], close_fds=True)
            self.after(0, self.destroy)
        except Exception as e:
            self.after(0, lambda: status_label.configure(text=f"Ошибка обновления: {e}", text_color="#ff6666"))

    # ===== HELPERS =====
    def open_esp_web(self):
        if ESP_IP:
            webbrowser.open(f"http://{ESP_IP}/")
            self.update_label.configure(text=f"Открываю веб-страницу ESP: {ESP_IP}")
        else:
            self.update_label.configure(text="ESP IP не выбран. Нажмите 'Найти ESP в сети'.")

    def schedule_after(self, delay_ms, callback):
        """Планирует UI-колбэк и позволяет отменить его при окончательном выходе."""
        if self._closing or not self.winfo_exists():
            return None
        after_id = None

        def guarded_callback():
            self._after_ids.discard(after_id)
            if not self._closing and self.winfo_exists():
                callback()

        after_id = self.after(delay_ms, guarded_callback)
        self._after_ids.add(after_id)
        return after_id

    def drain_ui_queue(self):
        """Выполняет результаты фоновых потоков только из главного Tk-потока."""
        if self._closing:
            return
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                pass
        self.schedule_after(100, self.drain_ui_queue)

    def safe_after_from_worker(self, callback):
        """Передаёт результат фонового потока в главный Tk-поток без вызова Tk из worker."""
        if not self._closing:
            self._ui_queue.put(callback)

    def shutdown(self):
        """Останавливает циклы UI и освобождает ресурсы перед уничтожением Tk."""
        if self._closing:
            return
        self._closing = True
        for after_id in tuple(self._after_ids):
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids.clear()
        if self.camera_reader:
            self.camera_reader.stop()
            self.camera_reader = None
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self.close_gadget()
        try:
            if self.db_cursor:
                self.db_cursor.close()
            if self.db_conn:
                self.db_conn.close()
        except Exception:
            pass
        super().destroy()

    def hide_to_tray(self):
        self.withdraw()

    def open_gadget(self):
        """Отдельный нерастягиваемый мини-гаджет для второго монитора."""
        if self.gadget and self.gadget.winfo_exists():
            self.gadget.deiconify()
            self.gadget.lift()
            self.gadget.focus_force()
            return
        gadget = ctk.CTkToplevel(self)
        self.gadget = gadget
        gadget.title("АТС · Гаджет")
        gadget.geometry("330x205")
        gadget.minsize(330, 205)
        gadget.maxsize(330, 205)
        gadget.resizable(False, False)
        gadget.attributes("-topmost", True)
        gadget.configure(fg_color=COLOR_BG)
        body = ctk.CTkFrame(gadget, fg_color=COLOR_SURFACE, corner_radius=16, border_width=1, border_color=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkLabel(body, text="АТС · МОНИТОРИНГ", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")).pack(anchor="w", padx=16, pady=(14, 2))
        self.gadget_temp = ctk.CTkLabel(body, text="--.- °C", text_color=COLOR_TEXT,
                                        font=ctk.CTkFont(family=FONT_FAMILY, size=38, weight="bold"))
        self.gadget_temp.pack(anchor="w", padx=16)
        self.gadget_meta = ctk.CTkLabel(body, text="Влажность: -- %   ·   ESP: --", text_color=COLOR_MUTED,
                                        font=ctk.CTkFont(family=FONT_FAMILY, size=13))
        self.gadget_meta.pack(anchor="w", padx=16, pady=(0, 10))
        self.gadget_door = ctk.CTkLabel(body, text="ДВЕРЬ: —", text_color=COLOR_MUTED,
                                        font=ctk.CTkFont(family=FONT_FAMILY, size=17, weight="bold"))
        self.gadget_door.pack(anchor="w", padx=16)
        ctk.CTkButton(body, text="Открыть программу", width=150, height=26,
                      fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT,
                      command=lambda: (self.deiconify(), self.lift())).pack(anchor="e", padx=14, pady=(1, 9))
        gadget.protocol("WM_DELETE_WINDOW", self.close_gadget)
        self.refresh_gadget()

    def close_gadget(self):
        if self.gadget and self.gadget.winfo_exists():
            self.gadget.destroy()
        self.gadget = None

    def refresh_gadget(self, temp=None, humidity=None, door_open=None, online=None):
        if not self.gadget or not self.gadget.winfo_exists():
            return
        if temp is not None:
            self.gadget_temp.configure(text=f"{temp:.1f} °C")
        if humidity is not None:
            self._gadget_humidity = humidity
        if online is not None:
            self._gadget_online = online
        if humidity is not None or online is not None:
            humidity_text = f"{getattr(self, '_gadget_humidity', 0):.0f}" if hasattr(self, '_gadget_humidity') else "--"
            online_text = "ONLINE" if getattr(self, '_gadget_online', False) else "OFFLINE"
            self.gadget_meta.configure(text=f"Влажность: {humidity_text} %   ·   ESP: {online_text}")
        if door_open is not None:
            self.gadget_door.configure(
                text=f"ДВЕРЬ: {'ОТКРЫТА' if door_open else 'ЗАКРЫТА'}",
                text_color=COLOR_DANGER if door_open else COLOR_SUCCESS,
            )

    def update_door_presentation(self, is_open):
        self.draw_door(is_open)
        if is_open:
            self.door_state_label.configure(text="ДВЕРЬ: ОТКРЫТА", text_color=COLOR_DANGER)
            self.status_frame.configure(border_color=COLOR_DANGER)
        else:
            self.door_state_label.configure(text="ДВЕРЬ: ЗАКРЫТА", text_color=COLOR_SUCCESS)
            self.status_frame.configure(border_color=COLOR_BORDER)

    def create_card(self, title):
        frame = ctk.CTkFrame(self.main, fg_color=COLOR_CARD, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        ctk.CTkLabel(
            frame, text=title, text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
        ).pack(anchor="w", padx=18, pady=(14, 0))
        return frame

    def draw_door(self, open_):
        img = self.door_img_open if open_ else self.door_img_closed
        self.door_canvas.itemconfig(self.door_icon, image=img)

    def update_graph(self):
        x = range(len(temp_history))
        self.temp_line.set_data(x, temp_history)
        self.hum_line.set_data(x, hum_history)
        self.ax.set_xlim(0, MAX_POINTS)
        self.ax.relim()
        self.ax.autoscale_view(scalex=False, scaley=True)
        self.canvas.draw_idle()

    # ===== MAIN LOOP =====
    def open_esp_selector(self):
        win = ctk.CTkToplevel(self)
        win.title("Подключение ESP")
        win.geometry("520x450")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(fg_color=COLOR_BG)
        body = ctk.CTkFrame(win, fg_color=COLOR_SURFACE, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=14, pady=14)

        stop_event = threading.Event()
        found_ips = []
        selected_ip = ctk.StringVar(value=ESP_IP or "")
        ctk.CTkLabel(body, text="Подключение к ESP", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=20, weight="bold")).pack(anchor="w", padx=18, pady=(18, 2))
        current_text = f"Текущий адрес: {ESP_IP or 'не выбран'}"
        current_label = ctk.CTkLabel(body, text=current_text, text_color=COLOR_MUTED,
                                     font=ctk.CTkFont(family=FONT_FAMILY, size=13))
        current_label.pack(anchor="w", padx=18, pady=(0, 16))
        ctk.CTkLabel(body, text="IP-адрес вручную", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")).pack(anchor="w", padx=18)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(4, 12))
        ip_entry = ctk.CTkEntry(row, textvariable=selected_ip, height=34, placeholder_text="192.168.88.30",
                                fg_color=COLOR_CARD_ALT, border_color=COLOR_BORDER, text_color=COLOR_TEXT)
        ip_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(row, text="Подключить", width=105, height=34, command=lambda: save_and_use(ip_entry.get()),
                      fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER).pack(side="right")
        status_label = ctk.CTkLabel(body, text="Поиск ещё не запущен", text_color=COLOR_MUTED,
                                    font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        status_label.pack(anchor="w", padx=18, pady=(0, 4))
        progress = ctk.CTkProgressBar(body, height=8, progress_color=COLOR_ACCENT, fg_color=COLOR_CARD_ALT)
        progress.set(0)
        progress.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkLabel(body, text="Найденные устройства", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")).pack(anchor="w", padx=18)
        devices_frame = ctk.CTkFrame(body, fg_color=COLOR_CARD_ALT, corner_radius=10)
        devices_frame.pack(fill="x", padx=18, pady=(4, 14))
        devices_combo = ctk.CTkComboBox(devices_frame, values=["Пока ничего не найдено"], variable=selected_ip,
                                        width=300, height=34, state="readonly", fg_color=COLOR_SURFACE,
                                        border_color=COLOR_BORDER, button_color=COLOR_ACCENT, dropdown_fg_color=COLOR_SURFACE,
                                        text_color=COLOR_TEXT)
        devices_combo.pack(side="left", fill="x", expand=True, padx=8, pady=8)

        scan_btn = None

        def save_and_use(ip):
            ip = ip.strip()
            if not ip:
                status_label.configure(text="Введите IP или дождитесь поиска", text_color=COLOR_WARNING)
                return

            set_esp_ip(ip)
            with open(ESP_CONFIG, "w", encoding="utf-8") as f:
                f.write(ip)

            status_label.configure(text=f"Используется {ip}", text_color=COLOR_SUCCESS)
            self.update_label.configure(text=f"ESP: {ip}")
            win.after(350, win.destroy)

        # Ручной адрес подключается основной кнопкой в этой строке.

        def add_found_ip(ip):
            if ip not in found_ips:
                found_ips.append(ip)
                devices_combo.configure(values=found_ips)
                selected_ip.set(ip)

        def do_scan():
            nonlocal scan_btn
            stop_event.clear()
            found_ips.clear()
            devices_combo.configure(values=["Поиск..."])
            selected_ip.set(ESP_IP or "")
            progress.set(0)
            status_label.configure(text="Быстрый поиск: последний IP, ats-room.local, затем подсеть...", text_color=COLOR_MUTED)
            if scan_btn:
                scan_btn.configure(state="disabled", text="Ищу...")

            def progress_cb(done, total, ip, ok):
                def update_progress():
                    if not win.winfo_exists():
                        return
                    progress.set(done / total if total else 0)
                    if ok:
                        status_label.configure(text=f"Найдена ESP: {ip}", text_color=COLOR_SUCCESS)
                        add_found_ip(ip)
                    else:
                        status_label.configure(text=f"Проверяю {ip}  ({done}/{total})", text_color=COLOR_MUTED)
                self.safe_after_from_worker(update_progress)

            def worker():
                devices = scan_for_esp(progress_cb=progress_cb, stop_event=stop_event)

                def finish():
                    if not win.winfo_exists():
                        return
                    if scan_btn:
                        scan_btn.configure(state="normal", text="Сканировать")
                    progress.set(1)
                    if devices:
                        for ip in devices:
                            add_found_ip(ip)
                        status_label.configure(text=f"Найдена ESP: {devices[0]}", text_color=COLOR_SUCCESS)
                        save_and_use(devices[0])
                    else:
                        devices_combo.configure(values=["Не найдено"])
                        status_label.configure(
                            text="ESP не найдена. Можно ввести IP вручную.",
                            text_color=COLOR_WARNING
                        )

                self.safe_after_from_worker(finish)

            threading.Thread(target=worker, daemon=True).start()

        def on_close():
            stop_event.set()
            win.destroy()

        btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        btn_frame.pack(fill="x", padx=18, pady=(0, 16))
        scan_btn = ctk.CTkButton(btn_frame, text="Сканировать", width=115, height=34, command=do_scan,
                                 fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER)
        scan_btn.pack(side="left")
        ctk.CTkButton(btn_frame, text="Использовать выбранный", width=160, height=34,
                      command=lambda: save_and_use(selected_ip.get()), fg_color=COLOR_CARD_ALT,
                      hover_color=COLOR_BORDER, text_color=COLOR_TEXT).pack(side="right")

        win.protocol("WM_DELETE_WINDOW", on_close)
        do_scan()


    def loop(self):
        global outside_temp, last_weather_update, season_mode

        # Сетевые запросы выполняются в worker-потоках: главный поток только рисует UI.
        now = time.time()
        if not self._esp_refresh_running:
            self._esp_refresh_running = True

            def read_esp():
                data = get_esp_data()
                def apply_esp():
                    self._latest_esp = data
                    self._latest_esp_at = time.time() if data else self._latest_esp_at
                    self._esp_refresh_running = False
                self.safe_after_from_worker(apply_esp)
            threading.Thread(target=read_esp, daemon=True).start()

        if now - last_weather_update > 900 and not self._weather_refresh_running:
            self._weather_refresh_running = True

            def read_weather():
                value = get_outside_temp()
                def apply_weather():
                    global outside_temp, last_weather_update, season_mode
                    outside_temp = value
                    season_mode = detect_season(outside_temp, season_mode)
                    last_weather_update = time.time()
                    self._weather_refresh_running = False
                self.safe_after_from_worker(apply_weather)
            threading.Thread(target=read_weather, daemon=True).start()

        esp = self._latest_esp

        if esp:
            self.last_esp_seen = now
            was_offline = not self.esp_online
            self.esp_online = True

            t = float(esp.get("room_temp", 0))
            h = float(esp.get("humidity", 0))
            p = float(esp.get("pressure", 0))
            d = bool(esp.get("door", False))

            self.room_label.configure(
                text=f"Температура: {t:.1f} °C     Улица: {outside_temp if outside_temp is not None else '--'} °C\n"
                     f"Влажность: {h:.0f} %     Давление: {p:.0f} мм"
            )
            self.status_label.configure(text=f"ESP: ONLINE\nРежим: {season_mode}", text_color=COLOR_SUCCESS)
            self.update_door_presentation(d)
            self.refresh_gadget(temp=t, humidity=h, door_open=d, online=True)

            temp_history.append(t)
            hum_history.append(h)

            self.draw_door(d)
            self.update_graph()

            self.update_label.configure(
                text=f"обновлено {time.strftime('%H:%M:%S')}"
            )

            check_alerts(
                room_temp=t,
                outside_temp=outside_temp,
                door_open=d,
                season=season_mode
            )

            # ✅ логирование с троттлингом
            if self.db_conn and self.db_cursor and now - self.last_logged >= self.LOG_INTERVAL:
                try:
                    with self.db_lock:
                        log_esp_data(
                            self.db_cursor,
                            temp=t,
                            hum=h,
                            press=p,
                            door=d,
                            online=True
                        )
                        self.db_conn.commit()
                    self.last_logged = now
                except Exception as e:
                    print("DB log error:", e)
                
            if was_offline:
                print("ESP снова ONLINE")

        else:
            if self.esp_online and now - self.last_esp_seen > self.ESP_TIMEOUT:
                self.esp_online = False
                print("ESP OFFLINE")

                if self.db_conn and self.db_cursor:
                    try:
                        with self.db_lock:
                            log_esp_data(
                                self.db_cursor,
                                temp=None,
                                hum=None,
                                press=None,
                                door=None,
                                online=False
                            )
                            self.db_conn.commit()
                    except Exception as e:
                        print("DB offline log error:", e)

            if not self.esp_online:
                self.door_state_label.configure(text="ДВЕРЬ: НЕТ ДАННЫХ", text_color=COLOR_MUTED)
                self.status_frame.configure(border_color=COLOR_BORDER)
                self.status_label.configure(text="ESP: OFFLINE\nРежим: —", text_color=COLOR_DANGER)
                self.refresh_gadget(online=False)
                self.update_label.configure(text="нет связи с ESP")

        if self._closing:
            return
        self.schedule_after(REFRESH_SEC * 1000, self.loop)



        
    def open_camera_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("Подключение камеры")
        win.geometry("460x390")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.configure(fg_color=COLOR_BG)
        body = ctk.CTkFrame(win, fg_color=COLOR_SURFACE, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=14, pady=14)
        ctk.CTkLabel(body, text="Подключение камеры", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=20, weight="bold")).pack(anchor="w", padx=18, pady=(18, 3))
        ctk.CTkLabel(body, text="Выберите сохранённый поток или укажите RTSP-адрес.", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=13)).pack(anchor="w", padx=18, pady=(0, 18))
        cam_var = ctk.StringVar(value=list(CAMERAS.keys())[0])
        ctk.CTkLabel(body, text="Сохранённая камера", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")).pack(anchor="w", padx=18)
        combo = ctk.CTkComboBox(body, values=list(CAMERAS.keys()), variable=cam_var, height=34,
                                fg_color=COLOR_CARD_ALT, border_color=COLOR_BORDER, button_color=COLOR_ACCENT,
                                dropdown_fg_color=COLOR_SURFACE, text_color=COLOR_TEXT)
        combo.pack(fill="x", padx=18, pady=(4, 14))
        ctk.CTkLabel(body, text="RTSP вручную", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")).pack(anchor="w", padx=18)
        manual_entry = ctk.CTkEntry(body, height=34, placeholder_text="rtsp://...", fg_color=COLOR_CARD_ALT,
                                    border_color=COLOR_BORDER, text_color=COLOR_TEXT)
        manual_entry.pack(fill="x", padx=18, pady=(4, 8))
        status = ctk.CTkLabel(body, text="", text_color=COLOR_WARNING, font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        status.pack(anchor="w", padx=18)

        def apply_camera():
            global CURRENT_RTSP
            selected = cam_var.get()
            manual = manual_entry.get().strip()
            if manual:
                CURRENT_RTSP = manual
            elif CAMERAS.get(selected):
                CURRENT_RTSP = CAMERAS[selected]
            else:
                status.configure(text="Выберите сохранённую камеру или введите RTSP-адрес")
                return
            self.start_camera(CURRENT_RTSP)
            win.destroy()

        controls = ctk.CTkFrame(body, fg_color="transparent")
        controls.pack(fill="x", padx=18, pady=(10, 16))
        ctk.CTkButton(controls, text="Подключить", width=115, height=34, command=apply_camera,
                      fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER).pack(side="left")
        ctk.CTkButton(controls, text="Отмена", width=80, height=34, command=win.destroy,
                      fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT).pack(side="right")
        
    def start_camera(self, rtsp_url):
        if self.camera_reader:
            self.camera_reader.stop()
        self.camera_reader = CameraReader(rtsp_url)
        self.schedule_after(150, self.update_camera)
        
    def update_camera(self):
        frame = self.camera_reader.latest_frame() if self.camera_reader else None
        if frame is not None:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            # Кадр строго совпадает с заранее заданной областью: layout не меняется при подключении.
            target_w, target_h = CAMERA_PREVIEW_SIZE
            src_h, src_w = frame.shape[:2]
            scale = max(target_w / src_w, target_h / src_h)
            scaled_w, scaled_h = round(src_w * scale), round(src_h * scale)
            frame = cv2.resize(frame, (scaled_w, scaled_h))
            x0 = max(0, (scaled_w - target_w) // 2)
            y0 = max(0, (scaled_h - target_h) // 2)
            frame = frame[y0:y0 + target_h, x0:x0 + target_w]
            img = ctk.CTkImage(light_image=Image.fromarray(frame), size=(target_w, target_h))
            self.cam_canvas.ctk_image = img
            self.cam_canvas.configure(image=img, text="")
        if not self._closing and self.camera_reader:
            self.schedule_after(80, self.update_camera)
    def open_camera_window(self):
        if not self.camera_reader or not self.camera_reader.is_opened():
            self.cam_canvas.configure(text="Камера не подключена")
            return

        win = ctk.CTkToplevel(self)
        win.title("Камера — полный просмотр")
        win.geometry("900x500")
        win.resizable(True, True)
        win.attributes("-topmost", True)

        cam_view = ctk.CTkLabel(win, text="", fg_color="black")
        cam_view.configure(text="")
        cam_view.pack(fill="both", expand=True)

        def update_big():
            if self._closing or not win.winfo_exists() or not cam_view.winfo_exists():
                return
            frame = self.camera_reader.latest_frame() if self.camera_reader else None
            if frame is not None:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                view_w = max(1, cam_view.winfo_width())
                view_h = max(1, cam_view.winfo_height())
                src_h, src_w = frame.shape[:2]
                # Для полного просмотра берём только готовый fit-размер: кадр никогда не масштабируется больше окна.
                scale = min(1.0, view_w / src_w, view_h / src_h)
                scaled_w, scaled_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
                frame = cv2.resize(frame, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
                img = ctk.CTkImage(light_image=Image.fromarray(frame), size=(scaled_w, scaled_h))
                cam_view.ctk_image = img
                cam_view.configure(width=view_w, height=view_h, image=img, text="")
            self.schedule_after(40, update_big)

        update_big()

    def destroy(self):
        self.shutdown()

    def open_history_window(self):
        win = ctk.CTkToplevel(self)
        win.title("История показаний")
        win.geometry("920x650")
        win.minsize(820, 560)
        win.attributes("-topmost", True)
        win.configure(fg_color=COLOR_BG)
        header = ctk.CTkFrame(win, fg_color=COLOR_SURFACE, corner_radius=0, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(header, text="История показаний", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=20, weight="bold")).pack(side="left", padx=20, pady=16)
        ctk.CTkLabel(header, text="Данные ESP", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=13)).pack(side="left", pady=18)
        content = ctk.CTkFrame(win, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=14, pady=12)
        filter_frame = ctk.CTkFrame(content, fg_color=COLOR_SURFACE, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        filter_frame.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(filter_frame, text="Период", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold")).pack(side="left", padx=(16, 8), pady=12)
        date_from = ctk.CTkEntry(filter_frame, width=170, height=32, placeholder_text="С: ГГГГ-ММ-ДД ЧЧ:ММ",
                                 fg_color=COLOR_CARD_ALT, border_color=COLOR_BORDER, text_color=COLOR_TEXT)
        date_from.pack(side="left", padx=4, pady=10)
        date_to = ctk.CTkEntry(filter_frame, width=170, height=32, placeholder_text="По: ГГГГ-ММ-ДД ЧЧ:ММ",
                               fg_color=COLOR_CARD_ALT, border_color=COLOR_BORDER, text_color=COLOR_TEXT)
        date_to.pack(side="left", padx=4, pady=10)

        graph_frame = ctk.CTkFrame(content, fg_color=COLOR_SURFACE, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        graph_frame.pack(fill="both", expand=True, pady=(0, 10))
        chart_header = ctk.CTkFrame(graph_frame, fg_color="transparent", height=30)
        chart_header.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(chart_header, text="Динамика показаний", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold")).pack(side="left")
        ctk.CTkLabel(chart_header, text="Температура / влажность", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12)).pack(side="right")
        fig, ax = plt.subplots(figsize=(8.2, 2.7), dpi=100)
        fig.patch.set_facecolor(COLOR_SURFACE)
        ax.set_facecolor(COLOR_CARD_ALT)
        ax.tick_params(colors=COLOR_MUTED)
        for spine in ax.spines.values():
            spine.set_color(COLOR_BORDER)
        canvas = FigureCanvasTkAgg(fig, master=graph_frame)
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=8, pady=8)
        canvas.get_tk_widget().configure(bg=COLOR_SURFACE, highlightthickness=0)

        # ===== ТАБЛИЦА =====
        table_frame = ctk.CTkFrame(content, fg_color=COLOR_SURFACE, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        table_frame.pack(fill="both", expand=False)
        table_header = ctk.CTkFrame(table_frame, fg_color="transparent", height=30)
        table_header.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(table_header, text="Последние показания", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold")).pack(side="left")
        ctk.CTkLabel(table_header, text="до 300 записей", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12)).pack(side="right")
        history_style = ttk.Style(win)
        history_style.theme_use("clam")
        history_style.configure("ATS.History.Treeview", background=COLOR_SURFACE, fieldbackground=COLOR_SURFACE,
                                foreground=COLOR_TEXT, rowheight=28, font=(FONT_FAMILY, 11), borderwidth=0)
        history_style.configure("ATS.History.Treeview.Heading", background="#E8EEF8", foreground=COLOR_MUTED,
                                font=(FONT_FAMILY, 11, "bold"), relief="flat", padding=(10, 8))
        history_style.map("ATS.History.Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", COLOR_TEXT)])
        table = ttk.Treeview(table_frame, style="ATS.History.Treeview", columns=("ts", "temp", "hum", "door"), show="headings", height=6)

        table.heading("ts", text="Время")
        table.heading("temp", text="Температура °C")
        table.heading("hum", text="Влажность %")
        table.heading("door", text="Дверь")

        table.column("ts", width=210, anchor="w")
        table.column("temp", width=170, anchor="center")
        table.column("hum", width=170, anchor="center")
        table.column("door", width=150, anchor="center")
        table.tag_configure("door_open", foreground=COLOR_DANGER)
        table.tag_configure("door_closed", foreground=COLOR_SUCCESS)
        table.pack(fill="x", padx=10, pady=(6, 10))

        # ===== ЛОГИКА =====
        def load_data():
            for i in table.get_children():
                table.delete(i)

            try:
                frm = date_from.get()
                to = date_to.get()

                if not self.db_conn:
                    print("History error: DB is not connected")
                    return

                with self.db_lock:
                    cur = self.db_conn.cursor(cursor_factory=RealDictCursor)
                    cur.execute("""
                        SELECT ts, temperature, humidity, door_open
                        FROM esp_log
                        WHERE ts BETWEEN %s AND %s
                        ORDER BY ts
                    """, (frm, to))
                    rows = cur.fetchall()
                    cur.close()
            except Exception as e:
                print("History error:", e)
                return

            if not rows:
                ax.clear()
                ax.text(0.5, 0.5, "Нет данных за выбранный период", ha="center", va="center")
                canvas.draw()
                return

            times = [r["ts"] for r in rows]
            temps = [r["temperature"] for r in rows]
            hums = [r["humidity"] for r in rows]

            ax.clear()
            ax.set_facecolor(COLOR_CARD_ALT)
            ax.plot(times, temps, label="Температура", color=COLOR_ACCENT, linewidth=2.4, marker="o", markersize=2.5)
            ax.fill_between(times, temps, color=COLOR_ACCENT, alpha=0.10)
            ax.plot(times, hums, label="Влажность", color=COLOR_WARNING, linewidth=2.2, marker="o", markersize=2.2)
            ax.set_ylabel("Значение", color=COLOR_MUTED)
            ax.tick_params(colors=COLOR_MUTED, labelsize=9)
            for spine in ax.spines.values():
                spine.set_color(COLOR_BORDER)
            ax.legend(loc="upper left", frameon=False, labelcolor=COLOR_TEXT, ncols=2)
            ax.grid(True, axis="y", alpha=0.65, color=COLOR_BORDER)
            ax.grid(False, axis="x")
            fig.autofmt_xdate()
            canvas.draw()

            for r in rows[-300:]:
                door_open = bool(r["door_open"])
                table.insert(
                    "", "end",
                    values=(
                        r["ts"].strftime("%d.%m.%Y  %H:%M"),
                        f"{r['temperature']:.1f} °C",
                        f"{r['humidity']:.0f} %",
                        "ОТКРЫТА" if door_open else "ЗАКРЫТА",
                    ),
                    tags=("door_open" if door_open else "door_closed",),
                )

        # ===== КНОПКИ ПЕРИОДОВ =====
        def set_range(hours):
            to = datetime.now()
            frm = to - timedelta(hours=hours)
            date_from.delete(0, "end")
            date_to.delete(0, "end")
            date_from.insert(0, frm.strftime("%Y-%m-%d %H:%M"))
            date_to.insert(0, to.strftime("%Y-%m-%d %H:%M"))
            load_data()

        ctk.CTkButton(filter_frame, text="Сегодня", width=76, height=32, command=lambda: set_range(24),
                      fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT).pack(side="left", padx=(12, 3))
        ctk.CTkButton(filter_frame, text="7 дней", width=70, height=32, command=lambda: set_range(24*7),
                      fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT).pack(side="left", padx=3)
        ctk.CTkButton(filter_frame, text="Обновить", width=92, height=32, command=load_data,
                      fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER).pack(side="right", padx=14)

        # ===== СТАРТ =====
        set_range(24)




class AlertWindow(ctk.CTkToplevel):
    def __init__(self, title, message, sound_file=None):
        super().__init__()
        self.sound_file = sound_file
        self._blink_state = False
        self.geometry("650x290")
        self.minsize(620, 270)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.attributes("-toolwindow", True)
        self.overrideredirect(True)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.configure(fg_color="#FFF7ED", border_width=5, border_color=COLOR_DANGER)

        ctk.CTkLabel(
            self, text=title, text_color=COLOR_DANGER,
            font=ctk.CTkFont(family=FONT_FAMILY, size=28, weight="bold"),
        ).pack(padx=30, pady=(34, 12))
        ctk.CTkLabel(
            self, text=message, justify="center", wraplength=570, text_color=COLOR_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=18),
        ).pack(padx=34, pady=(0, 16))
        ctk.CTkLabel(
            self, text="Уведомление исчезнет только после изменения состояния двери.",
            text_color=COLOR_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=13),
        ).pack(padx=20, pady=(0, 26))

        self.update_idletasks()
        w, h = 650, 290
        x = (self.winfo_screenwidth() - w) // 2
        y = max(50, (self.winfo_screenheight() - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.after(300, self._blink_border)
        self.after(1000, self._sound_loop)

    # ================= ВСПОМОГАТЕЛЬНОЕ =================

    def _is_night(self):
        hour = time.localtime().tm_hour
        return hour >= 23 or hour < 8

    def _blink_border(self):
        self._blink_state = not self._blink_state
        color = COLOR_DANGER if self._blink_state else COLOR_WARNING
        self.configure(border_color=color)
        self.after(700, self._blink_border)

    def _sound_loop(self):
        if not self._is_night():
            try:
                if self.sound_file and os.path.exists(self.sound_file):
                    winsound.PlaySound(
                        self.sound_file,
                        winsound.SND_FILENAME | winsound.SND_ASYNC
                    )
            except Exception:
                pass

        # 🔁 повтор каждые 5 минут
        self.after(5 * 60 * 1000, self._sound_loop)

class AlertManager:
    def __init__(self):
        self.alerts = {}

    def show(self, key, title, message):
        if key in self.alerts:
            return
        win = AlertWindow(
            title,
            message,
            sound_file=os.path.join(BASE_DIR, "sounds", "winter_vent.wav")
        )
        self.alerts[key] = win

    def clear(self, key):
        win = self.alerts.pop(key, None)
        if win:
            win.destroy()


# ✅ ВОТ ЭТА СТРОКА ОБЯЗАТЕЛЬНА
alert_manager = AlertManager()

# ===== загрузка последнего ESP =====
if os.path.exists(ESP_CONFIG):
    try:
        with open(ESP_CONFIG, "r") as f:
            ip = f.read().strip()
            if ip:
                set_esp_ip(ip)
    except Exception:
        set_esp_ip(None)

# ================= START =================

if __name__ == "__main__":
    app = ATSApp()

    def run_tray():
        image = Image.open(os.path.join(BASE_DIR, "icons", "tray.png"))
        menu = (
            item("Открыть", tray_show),
            item("Выход", tray_quit),
        )
        tray = pystray.Icon("ats_monitor", image, "ATS Monitor", menu)
        tray.run()


    threading.Thread(target=run_tray, daemon=True).start()

    app.mainloop()

