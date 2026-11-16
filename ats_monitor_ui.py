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
import json
import math
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
from fan_controller import FanClient, normalize_endpoint, status_settings, parse_settings_form

if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    USER_DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    USER_DATA_DIR = BASE_DIR


# ================= НАСТРОЙКИ =================

APP_NAME = "ATS Monitor"
APP_VERSION = "1.5.0"
UPDATE_REPO = "ogureczloi-commits/ATS_Monitor_releases"
UPDATE_API_URL = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_CHECK_INTERVAL = 24 * 60 * 60

ESP_CONFIG = os.path.join(USER_DATA_DIR, "esp_last_ip.txt")
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


MAX_POINTS = 60
temp_history = deque(maxlen=MAX_POINTS)
hum_history  = deque(maxlen=MAX_POINTS)

# ================= ДАННЫЕ =================

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


def probe_esp_devices(address, timeout=ESP_SCAN_TIMEOUT):
    """Identify each independent device only from its own status contract."""
    found = []
    sensor = probe_esp_ip(address, timeout=timeout)
    if sensor:
        found.append(('sensor', str(address)))
    try:
        status = FanClient(address, timeout=timeout).status()
        if isinstance(status.get('room_valid'), bool) and isinstance(status.get('outdoor_valid'), bool):
            found.append(('fan', str(address)))
    except Exception:
        pass
    return found


def scan_for_devices(progress_cb=None, stop_event=None, fan_endpoint=''):
    candidates = build_scan_candidates()
    if fan_endpoint:
        from urllib.parse import urlsplit
        host = urlsplit(normalize_endpoint(fan_endpoint)).netloc
        if host and host not in candidates:
            candidates.insert(0, host)
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=ESP_SCAN_WORKERS) as executor:
        futures = {executor.submit(probe_esp_devices, ip, ESP_SCAN_TIMEOUT): ip for ip in candidates}
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            if stop_event and stop_event.is_set():
                executor.shutdown(wait=False, cancel_futures=True)
                return found
            ip = futures[future]
            try:
                devices = future.result()
            except Exception:
                devices = []
            found.extend(devices)
            if progress_cb:
                progress_cb(done, len(candidates), ip, devices)
    return found


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


def validated_esp(data):
    if not is_esp_status(data):
        return None
    result = dict(data)
    try:
        for key in ('room_temp', 'humidity', 'pressure'):
            value = data.get(key, 0)
            if isinstance(value, bool):
                return None
            result[key] = float(value)
            if not math.isfinite(result[key]):
                return None
    except (TypeError, ValueError, OverflowError):
        return None
    return result


def get_esp_data():
    if not ESP_URL:
        return None
    try:
        r = requests.get(ESP_URL, timeout=0.8, headers={"Connection": "close"})
        if r.status_code == 200:
            data = r.json()
            if is_esp_status(data):
                return validated_esp(data)
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
        self._frame_at = 0.0
        self._thread = threading.Thread(target=self._run, args=(rtsp_url,), daemon=True)
        self._thread.start()

    def _run(self, rtsp_url):
        while not self._stop.is_set():
            cap = None
            try:
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG, [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])
                with self._lock:
                    self._opened = cap.isOpened()
                while not self._stop.is_set() and cap.isOpened():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        break
                    with self._lock:
                        self._frame = frame
                        self._frame_at = time.monotonic()
            except Exception:
                pass
            finally:
                if cap is not None:
                    cap.release()
                with self._lock:
                    self._opened = False
                    self._frame = None
            self._stop.wait(2)

    def latest_frame(self):
        with self._lock:
            return None if self._frame is None or time.monotonic() - self._frame_at > 5 else self._frame.copy()

    def is_opened(self):
        with self._lock:
            return self._opened

    def stop(self):
        self._stop.set()


# ================= УЛИЧНЫЙ ДАТЧИК ВЕНТИЛЯТОРА =================

def outdoor_from_fan(status):
    if not isinstance(status, dict) or status.get('online') is not True or status.get('outdoor_valid') is not True:
        return None
    value = status.get('outdoor_c')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -55 <= value <= 125:
        return None
    return value

def detect_season(out_temp, prev):
    if out_temp is None:
        return "—"
    if out_temp < 10:
        return "ЗИМА"
    if out_temp > 15:
        return "ЛЕТО"
    return prev


def check_alerts(room_temp, outside_temp, door_open, season):
    if season == "ЗИМА" and outside_temp is not None and room_temp is not None and door_open is not None:
        if outside_temp < 0 and room_temp > 23 and not door_open:
            alert_manager.show(
                "winter_vent",
                "⚠️ Требуется проветривание",
                "Температура в помещении АТС растёт при отрицательной температуре на улице.\n\nОткройте дверь для проветривания."
            )
        else:
            alert_manager.clear("winter_vent")
    else:
        alert_manager.clear("winter_vent")


    # ☀️ ЛЕТО — дверь открыта долго
    if season == "ЛЕТО" and outside_temp is not None and door_open is not None:
        if door_open:
            alert_manager.show(
                "door_summer",
                "🚪 Дверь открыта",
                "Дверь открыта при включённом кондиционере.\n\nЗакройте дверь для сохранения охлаждения."
            )
        else:
            alert_manager.clear("door_summer")
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
        password=DB_PASS, connect_timeout=5, options="-c statement_timeout=5000"
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
        self.fan_endpoint_file = os.path.join(os.environ.get("LOCALAPPDATA", USER_DATA_DIR), "ATS_Monitor", "fan_endpoint.txt")
        try:
            with open(self.fan_endpoint_file, encoding="utf-8") as f:
                self.fan_endpoint = normalize_endpoint(f.read())
        except (OSError, ValueError):
            self.fan_endpoint = ""
        self.fan_status = None
        self._fan_last_poll_at = 0.0
        self._fan_busy = False
        self._fan_generation = 0
        self._fan_angle = 0.0
        # БД подключается после отрисовки окна, чтобы недоступная сеть не подвешивала запуск.
        self.db_conn = None
        self.db_cursor = None
        self.db_connecting = False
        self.db_lock = threading.Lock()
        self._db_log_busy = False
        self._db_log_queue = queue.Queue()
        self._db_log_lock = threading.Lock()
        self._db_enabled = bool(DB_HOST and DB_NAME and DB_USER)
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
        ctk.CTkButton(top_buttons, text="Вентиляция", command=self.open_fan_settings, width=92,
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

        # ===== Мелкий статус обновления =====
        self.update_label = ctk.CTkLabel(
            self.main,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="#888888"
        )
        self.update_label.grid(row=2, column=1, sticky="e", padx=20)

        self.schedule_after(100, self.drain_ui_queue)
        self.schedule_after(1000, self.loop)
        self.schedule_after(500, self.poll_fan)
        self.schedule_after(3000, lambda: self.check_for_updates(manual=False))
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

    def fan_request(self, operation, done=None):
        if self._fan_busy or not self.fan_endpoint:
            if done:
                done(False, "Нет адреса или запрос уже выполняется")
            return
        self._fan_busy = True
        endpoint, generation = self.fan_endpoint, self._fan_generation

        def worker():
            try:
                result = operation(FanClient(endpoint))
                error = None
            except Exception as exc:
                result, error = None, str(exc)
            completed_at = time.monotonic()

            def apply():
                self._fan_busy = False
                if generation != self._fan_generation:
                    return
                if error is None and isinstance(result, dict) and result.get('online'):
                    self._fan_last_poll_at = completed_at
                if done:
                    done(error is None, result if error is None else error)
            self.safe_after_from_worker(apply)
        threading.Thread(target=worker, daemon=True).start()

    def poll_fan(self):
        now = time.monotonic()
        if self.fan_status and (not self._fan_last_poll_at or now - self._fan_last_poll_at > 12):
            self.fan_status = None
            self.update_fan_dialog()
        if self.fan_endpoint and not self._fan_busy and now >= getattr(self, '_fan_next_poll_at', 0):
            self._fan_next_poll_at = now + 2
            def complete(ok, result):
                # Leave breathing room after slow responses and failures too.
                self._fan_next_poll_at = time.monotonic() + 2
                if ok:
                    self.fan_status = result if result.get('online') and time.monotonic() - self._fan_last_poll_at <= 12 else None
                elif not self._fan_last_poll_at or time.monotonic() - self._fan_last_poll_at > 12:
                    self.fan_status = None
                self.update_fan_dialog()
            self.fan_request(lambda client: client.status(), complete)
        self.schedule_after(250, self.poll_fan)

    def animate_fan(self, gadget):
        if self.gadget is not gadget or not gadget.winfo_exists():
            return
        status = self.fan_status
        speed = status['output_speed'] if status else 0
        now = time.monotonic()
        previous = getattr(self, '_fan_animation_at', now)
        self._fan_animation_at = now
        self._fan_angle = (self._fan_angle + speed * 0.05 * max(0, now - previous)) % (2 * math.pi)
        canvas = self.fan_canvas
        canvas.delete('all')
        # Original vector artwork: a shrouded, three-blade axial fan.
        canvas.create_oval(1, 1, 41, 41, fill='#edf3fa', outline='#cbd9e8', width=1)
        canvas.create_oval(4, 4, 38, 38, fill='#f8fbff', outline='#dce7f2', width=1)

        def blades(angle, color):
            for i in range(3):
                a = angle + i * 2 * math.pi / 3
                points = []
                for radius, offset in ((3, -0.48), (9, -0.62), (16, -0.38),
                                       (17, 0.10), (14, 0.46), (8, 0.40), (3, 0.25)):
                    points.extend((21 + radius * math.cos(a + offset),
                                   21 + radius * math.sin(a + offset)))
                canvas.create_polygon(*points, fill=color, outline='', smooth=True, splinesteps=18)

        # Pale angular echoes suggest motion, not measured RPM.
        if speed > 0:
            blades(self._fan_angle - min(0.52, 0.12 + speed * 0.004), '#e1eefb')
            blades(self._fan_angle - min(0.30, 0.06 + speed * 0.002), '#bcdcf6')
        blades(self._fan_angle, '#438fd0' if status else '#a6b5c4')
        canvas.create_oval(17, 17, 25, 25, fill='#f8fbff', outline='#91adc6', width=1)
        canvas.create_oval(19, 19, 23, 23, fill='#79a8cf' if status else '#a6b5c4', outline='')
        # CTkCanvas uses physical pixels, unlike CTk widgets; scale the artwork
        # to its actual requested size so it stays crisp at Windows DPI scales.
        scale = canvas.winfo_reqwidth() / 42
        canvas.scale('all', 0, 0, scale, scale)
        self.fan_gadget_label.configure(text=(f"Вентиляция: {speed:g}%\n{'Автоматический' if status['mode'] == 'auto' else 'Ручной'} режим" if status else "Вентиляция\nНет связи"))
        self.schedule_after(33, lambda: self.animate_fan(gadget))

    def update_fan_dialog(self):
        window = getattr(self, '_fan_window', None)
        if not window or not window.winfo_exists():
            return
        status = self.fan_status
        if status:
            room = f"{status['room_c']:.1f} °C" if status.get('room_valid') and isinstance(status.get('room_c'), (int, float)) and math.isfinite(status['room_c']) else 'нет данных'
            outdoor = f"{status['outdoor_c']:.1f} °C" if status.get('outdoor_valid') and isinstance(status.get('outdoor_c'), (int, float)) and math.isfinite(status['outdoor_c']) else 'нет данных'
            self._fan_connection_label.configure(text='●  На связи', text_color=COLOR_SUCCESS)
            if not status.get('alarm_known'):
                alarm_text, alarm_color = 'Авария: неизвестно', COLOR_WARNING
            elif status.get('alarm'):
                alarm_text, alarm_color = '●  Авария', '#DC2626'
            else:
                alarm_text, alarm_color = '●  Аварии нет', COLOR_SUCCESS
            self._fan_alarm_label.configure(text=alarm_text, text_color=alarm_color)
            self._fan_output_label.configure(text=f"{status['output_speed']:g}%")
            self._fan_current_mode_label.configure(text='Автоматический' if status['mode'] == 'auto' else 'Ручной')
            self._fan_temperatures_label.configure(text=f'Помещение: {room}    ·    Улица: {outdoor}')
            settings = status_settings(status)
            if not self._fan_mode_initialized:
                self._fan_mode.set('Ручной' if status['mode'] == 'manual' else 'Автоматический')
                requested = status.get('requested_speed')
                if isinstance(requested, (int, float)) and 0 <= requested <= 100:
                    self._fan_speed.set(requested)
                    self._fan_speed_label.configure(text=f'Ручная мощность: {int(requested)}%')
                self._fan_mode_initialized = True
            if len(settings) == len(self._fan_fields) and not self._fan_settings_dirty:
                for key, entry in self._fan_fields.items():
                    entry.delete(0, 'end')
                    entry.insert(0, str(settings[key]))
                self._fan_settings_baseline = settings.copy()
            self._fan_settings_loaded = len(settings) == len(self._fan_fields)
            self._fan_save_button.configure(state='normal' if self._fan_settings_loaded else 'disabled')
        else:
            self._fan_settings_loaded = False
            self._fan_save_button.configure(state='disabled')
            self._fan_connection_label.configure(text='●  Нет связи', text_color=COLOR_WARNING)
            self._fan_alarm_label.configure(text='Авария: неизвестно', text_color=COLOR_MUTED)
            self._fan_output_label.configure(text='—')
            self._fan_current_mode_label.configure(text='Нет данных')
            self._fan_temperatures_label.configure(text='Помещение: нет данных    ·    Улица: нет данных')

    def open_fan_settings(self):
        window = getattr(self, '_fan_window', None)
        if window and window.winfo_exists():
            window.deiconify()
            window.lift()
            window.focus_force()
            return
        window = ctk.CTkToplevel(self)
        window.transient(self)
        self._fan_window = window
        self._fan_settings_loaded = False
        self._fan_settings_dirty = False
        self._fan_settings_baseline = {}
        self._fan_mode_initialized = False
        window.title('Вентилятор · настройки')
        window.geometry('600x600')
        window.minsize(520, 480)
        window.configure(fg_color=COLOR_BG)
        header = ctk.CTkFrame(window, fg_color=COLOR_SURFACE, corner_radius=0, height=58)
        header.pack(fill='x')
        header.pack_propagate(False)
        ctk.CTkLabel(header, text='Вентилятор', text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight='bold')).pack(side='left', padx=(18, 10))
        ctk.CTkLabel(header, text='Состояние и управление', text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=13)).pack(side='left')
        body = ctk.CTkScrollableFrame(window, fg_color=COLOR_BG, corner_radius=0,
                                      scrollbar_button_color=COLOR_BORDER, scrollbar_button_hover_color=COLOR_MUTED)
        body.pack(fill='both', expand=True, padx=8, pady=(10, 8))
        status_card = ctk.CTkFrame(body, fg_color=COLOR_CARD, corner_radius=14,
                                   border_width=1, border_color=COLOR_BORDER)
        status_card.pack(fill='x', padx=4, pady=(0, 10))
        ctk.CTkLabel(status_card, text='Состояние контроллера', text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight='bold')).pack(anchor='w', padx=18, pady=(14, 2))
        control_card = ctk.CTkFrame(body, fg_color=COLOR_CARD, corner_radius=14,
                                    border_width=1, border_color=COLOR_BORDER)
        control_card.pack(fill='x', padx=4, pady=(0, 10))
        ctk.CTkLabel(control_card, text='Управление', text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight='bold')).pack(anchor='w', padx=18, pady=(14, 2))
        ctk.CTkLabel(control_card, text='Заданная мощность, не измеренные обороты', text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12)).pack(anchor='w', padx=18, pady=(0, 7))
        button_font = ctk.CTkFont(family=FONT_FAMILY, size=12)
        field_font = ctk.CTkFont(family=FONT_FAMILY, size=12)
        primary = dict(height=32, corner_radius=8, fg_color=COLOR_ACCENT,
                       hover_color=COLOR_ACCENT_HOVER, font=button_font)
        secondary = dict(height=32, corner_radius=8, fg_color=COLOR_CARD_ALT,
                         hover_color=COLOR_BORDER, text_color=COLOR_TEXT, font=button_font)
        entry_style = dict(height=34, fg_color=COLOR_CARD_ALT, border_color=COLOR_BORDER,
                           text_color=COLOR_TEXT, font=field_font)
        message = ctk.CTkLabel(control_card, text='', anchor='w', justify='left', wraplength=480,
                               text_color=COLOR_WARNING, font=field_font)
        def save_endpoint():
            try:
                value = normalize_endpoint(endpoint.get())
                os.makedirs(os.path.dirname(self.fan_endpoint_file), exist_ok=True)
                with open(self.fan_endpoint_file, 'w', encoding='utf-8') as f:
                    f.write(value)
            except (OSError, ValueError) as exc:
                message.configure(text=f'Ошибка: {exc}')
                return
            self.fan_endpoint = value
            self._fan_generation += 1
            self.fan_status = None
            self._fan_settings_loaded = False
            self._fan_settings_dirty = False
            self._fan_settings_baseline = {}
            self._fan_mode_initialized = False
            self.update_fan_dialog()
            message.configure(text='Адрес сохранён; ожидание опроса')
        status_line = ctk.CTkFrame(status_card, fg_color='transparent')
        status_line.pack(fill='x', padx=18, pady=(2, 6))
        self._fan_connection_label = ctk.CTkLabel(status_line, text='', font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight='bold'))
        self._fan_connection_label.pack(side='left')
        self._fan_alarm_label = ctk.CTkLabel(status_line, text='', font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight='bold'))
        self._fan_alarm_label.pack(side='right')
        metrics = ctk.CTkFrame(status_card, fg_color=COLOR_CARD_ALT, corner_radius=10)
        metrics.pack(fill='x', padx=18, pady=(0, 4))
        output = ctk.CTkFrame(metrics, fg_color='transparent')
        output.pack(side='left', fill='x', expand=True, padx=12, pady=5)
        ctk.CTkLabel(output, text='КОМАНДА ВЫХОДА', text_color=COLOR_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=11)).pack(anchor='w')
        self._fan_output_label = ctk.CTkLabel(output, text='—', text_color=COLOR_ACCENT, font=ctk.CTkFont(family=FONT_FAMILY, size=23, weight='bold'))
        self._fan_output_label.pack(anchor='w')
        current_mode = ctk.CTkFrame(metrics, fg_color='transparent')
        current_mode.pack(side='left', fill='x', expand=True, padx=12, pady=5)
        ctk.CTkLabel(current_mode, text='РЕЖИМ КОНТРОЛЛЕРА', text_color=COLOR_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=11)).pack(anchor='w')
        self._fan_current_mode_label = ctk.CTkLabel(current_mode, text='—', text_color=COLOR_TEXT, font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight='bold'))
        self._fan_current_mode_label.pack(anchor='w')
        self._fan_temperatures_label = ctk.CTkLabel(status_card, text='', anchor='w', text_color=COLOR_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        self._fan_temperatures_label.pack(fill='x', padx=18, pady=(0, 10))
        mode = ctk.CTkSegmentedButton(control_card, values=['Автоматический', 'Ручной'],
                                      height=32, corner_radius=8, font=field_font,
                                      fg_color=COLOR_CARD_ALT, selected_color='#BFDBFE',
                                      selected_hover_color='#DBEAFE',
                                      unselected_color=COLOR_CARD_ALT, unselected_hover_color=COLOR_BORDER,
                                      text_color=COLOR_TEXT, text_color_disabled=COLOR_MUTED)
        mode.set('Автоматический')
        mode.pack(fill='x', padx=18, pady=3)
        speed = ctk.CTkSlider(control_card, from_=0, to=100, number_of_steps=100,
                              fg_color=COLOR_CARD_ALT, progress_color=COLOR_ACCENT,
                              button_color=COLOR_ACCENT, button_hover_color=COLOR_ACCENT_HOVER)
        speed.set(0)
        speed_label = ctk.CTkLabel(control_card, text='Ручная мощность: 0%',
                                   text_color=COLOR_TEXT, font=field_font)
        self._fan_mode, self._fan_speed, self._fan_speed_label = mode, speed, speed_label
        speed.configure(command=lambda value: speed_label.configure(text=f'Ручная мощность: {int(value)}%'))
        speed_label.pack(anchor='w', padx=18, pady=(7, 0))
        speed.pack(fill='x', padx=18, pady=(3, 10))
        def complete(ok, result, saved=False):
            self.fan_status = result if ok and result.get('online') and time.monotonic() - self._fan_last_poll_at <= 12 else None
            if window.winfo_exists() and self._fan_window is window:
                if saved and ok:
                    self._fan_settings_dirty = False
                message.configure(text='Состояние получено от контроллера' if ok else f'Ошибка: {result}')
            self.update_fan_dialog()
        def control():
            selected_mode = 'manual' if mode.get() == 'Ручной' else 'auto'
            value = int(round(speed.get())) if selected_mode == 'manual' else None
            self.fan_request(lambda client: client.control(selected_mode, value), complete)
        ctk.CTkButton(control_card, text='Применить режим', command=control, **primary).pack(anchor='w', padx=18, pady=(0, 4))
        message.pack(fill='x', padx=18, pady=(0, 12))
        advanced = ctk.CTkFrame(body, fg_color=COLOR_CARD, corner_radius=14,
                                border_width=1, border_color=COLOR_BORDER)
        def toggle_advanced():
            if advanced.winfo_manager():
                advanced.pack_forget()
                advanced_button.configure(text='▶  Дополнительные настройки')
            else:
                advanced.pack(fill='x', padx=4, pady=(0, 8))
                advanced_button.configure(text='▼  Дополнительные настройки')
        advanced_button = ctk.CTkButton(body, text='▶  Дополнительные настройки',
                                        command=toggle_advanced, anchor='w', **secondary)
        advanced_button.pack(fill='x', padx=4, pady=(0, 8))
        ctk.CTkLabel(advanced, text='Адрес контроллера (http://IP:порт); пусто — отключить', anchor='w',
                     text_color=COLOR_MUTED, font=field_font).pack(fill='x', padx=18, pady=(14, 2))
        endpoint = ctk.CTkEntry(advanced, **entry_style)
        endpoint.pack(fill='x', padx=18)
        endpoint.insert(0, self.fan_endpoint)
        ctk.CTkButton(advanced, text='Сохранить адрес', command=save_endpoint, **secondary).pack(anchor='w', padx=18, pady=8)
        ctk.CTkLabel(advanced, text='Настройки автоматики и датчиков', anchor='w',
                     text_color=COLOR_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight='bold')).pack(fill='x', padx=18, pady=(8, 4))
        self._fan_fields = {}
        fields = [('stop_c', 'Температура остановки, °C'), ('start_c', 'Температура запуска, °C'),
                  ('max_c', 'Температура максимальной мощности, °C'), ('delta_c', 'Улица холоднее минимум на, °C'),
                  ('alarm_c', 'Температура аварии, °C'), ('min_power', 'Минимальная мощность, %'),
                  ('max_power', 'Максимальная мощность, %'), ('room_rom', 'ROM датчика помещения'),
                  ('outdoor_rom', 'ROM уличного датчика')]
        def mark_dirty(_event):
            self._fan_settings_dirty = True
        for key, title in fields:
            row = ctk.CTkFrame(advanced, fg_color='transparent')
            row.pack(fill='x', padx=18, pady=3)
            ctk.CTkLabel(row, text=title, anchor='w', width=260, text_color=COLOR_TEXT, font=field_font).pack(side='left')
            entry = ctk.CTkEntry(row, **entry_style)
            entry.pack(side='left', fill='x', expand=True)
            entry.bind('<KeyRelease>', mark_dirty)
            self._fan_fields[key] = entry
        def reload_settings():
            if not self.fan_status:
                message.configure(text='Нет актуальных настроек контроллера')
                return
            self._fan_settings_dirty = False
            self.update_fan_dialog()
            message.configure(text='Показаны последние полученные настройки')
        def save_settings():
            try:
                if not self.fan_status or not self._fan_settings_loaded:
                    raise ValueError('Сначала получите актуальные настройки')
                if status_settings(self.fan_status) != self._fan_settings_baseline:
                    raise ValueError('Настройки изменились на контроллере; обновите форму')
                payload = parse_settings_form({key: entry.get() for key, entry in self._fan_fields.items()})
            except ValueError as exc:
                message.configure(text=f'Ошибка: {exc}')
                return
            baseline = self._fan_settings_baseline.copy()
            def save_if_unchanged(client):
                latest = client.status()
                if status_settings(latest) != baseline:
                    raise ValueError('Настройки изменились на контроллере; обновите форму')
                return client.settings(payload)
            self.fan_request(save_if_unchanged, lambda ok, result: complete(ok, result, saved=True))
        actions = ctk.CTkFrame(advanced, fg_color='transparent')
        actions.pack(fill='x', padx=18, pady=(10, 16))
        self._fan_save_button = ctk.CTkButton(actions, text='Сохранить настройки', command=save_settings, state='disabled', **primary)
        self._fan_save_button.pack(side='left', padx=(0, 8))
        ctk.CTkButton(actions, text='Обновить из контроллера', command=reload_settings, **secondary).pack(side='left')
        self.update_fan_dialog()
        window.update_idletasks()
        window.lift()
        window.focus_force()

    def log_data_async(self, **values):
        if self._closing:
            return
        # Queue state transitions too: a busy write must not discard OFFLINE.
        with self._db_log_lock:
            self._db_log_queue.put(values)
            if self._db_log_busy:
                return
            self._db_log_busy = True
        def worker():
            while True:
                with self._db_log_lock:
                    if self._closing or self._db_log_queue.empty():
                        self._db_log_busy = False
                        return
                    values = self._db_log_queue.get_nowait()
                try:
                    conn = get_db_conn()
                    try:
                        with conn:
                            with conn.cursor() as cur:
                                log_esp_data(cur, **values)
                    finally:
                        conn.close()
                except Exception as exc:
                    # A later sample opens a fresh connection, even after startup failure.
                    print("DB log error:", exc)
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
                bad_file = zf.testzip()
                if bad_file:
                    raise RuntimeError(f"Повреждён файл в архиве: {bad_file}")

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
        self._fan_animation_at = time.monotonic()
        gadget.title("АТС · Гаджет")
        gadget.geometry("330x285")
        gadget.minsize(330, 285)
        gadget.maxsize(330, 285)
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
        fan_row = ctk.CTkFrame(body, fg_color="transparent")
        fan_row.pack(fill="x", padx=16)
        icon_size = round(60 * fan_row._get_widget_scaling())
        self.fan_canvas = ctk.CTkCanvas(fan_row, width=icon_size, height=icon_size, bg=COLOR_SURFACE, highlightthickness=0)
        self.fan_canvas.pack(side="left")
        self.fan_gadget_label = ctk.CTkLabel(fan_row, text="Вентиляция · нет связи", text_color=COLOR_MUTED,
                                              font=ctk.CTkFont(family=FONT_FAMILY, size=11), justify="left", anchor="w")
        self.fan_gadget_label.pack(side="left", padx=8)
        ctk.CTkButton(body, text="Открыть программу", width=150, height=26,
                      fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT,
                      command=lambda: (self.deiconify(), self.lift())).pack(anchor="e", padx=14, pady=(1, 9))
        gadget.protocol("WM_DELETE_WINDOW", self.close_gadget)
        self.refresh_gadget()
        self.animate_fan(gadget)

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
        win.geometry("560x540")
        win.minsize(540, 520)
        win.transient(self)
        win.configure(fg_color=COLOR_BG)
        body = ctk.CTkFrame(win, fg_color=COLOR_SURFACE, corner_radius=14, border_width=1, border_color=COLOR_BORDER)
        body.pack(fill="both", expand=True, padx=14, pady=14)
        stop_event = threading.Event()
        found = {}
        choice = ctk.StringVar(value="")
        address = ctk.StringVar(value="")
        kind = ctk.StringVar(value="Датчик АТС")
        ctk.CTkLabel(body, text="Подключение ESP", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=20, weight="bold")).pack(anchor="w", padx=18, pady=(16, 2))
        ctk.CTkLabel(body, text="Датчик помещения и контроллер вентиляции", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=12)).pack(anchor="w", padx=18, pady=(0, 10))
        current_card = ctk.CTkFrame(body, fg_color=COLOR_CARD_ALT, corner_radius=10)
        current_card.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkLabel(current_card, text="ТЕКУЩИЕ ПОДКЛЮЧЕНИЯ", text_color=COLOR_MUTED,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold")).pack(anchor="w", padx=12, pady=(8, 0))
        current = ctk.CTkLabel(current_card, text="", text_color=COLOR_TEXT, justify="left", anchor="w",
                               font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        current.pack(fill="x", padx=12, pady=(0, 8))
        def refresh_current():
            current.configure(text=f"Датчик АТС: {ESP_IP or 'не выбран'}\nВентилятор: {self.fan_endpoint or 'не выбран'}")
        refresh_current()
        ctk.CTkLabel(body, text="Подключить по адресу", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold")).pack(anchor="w", padx=18)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(4, 8))
        ctk.CTkOptionMenu(row, variable=kind, values=["Датчик АТС", "Вентилятор"], width=145,
                          height=32, corner_radius=8, fg_color=COLOR_CARD_ALT, button_color=COLOR_BORDER,
                          button_hover_color=COLOR_ACCENT, text_color=COLOR_TEXT).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(row, textvariable=address, placeholder_text="IP или адрес:порт", height=32,
                     corner_radius=8, border_color=COLOR_BORDER).pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(body, text="Поиск в сети", text_color=COLOR_TEXT,
                     font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold")).pack(anchor="w", padx=18)
        status = ctk.CTkLabel(body, text="Поиск ещё не запущен", text_color=COLOR_MUTED,
                              font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        status.pack(anchor="w", padx=18, pady=(8, 4))
        progress = ctk.CTkProgressBar(body, height=8, progress_color=COLOR_ACCENT, fg_color=COLOR_CARD_ALT)
        progress.set(0)
        progress.pack(fill="x", padx=18, pady=(0, 12))
        combo = ctk.CTkComboBox(body, values=["Пока ничего не найдено"], variable=choice, state="readonly",
                                height=32, corner_radius=8, border_color=COLOR_BORDER,
                                button_color=COLOR_CARD_ALT, button_hover_color=COLOR_BORDER,
                                text_color=COLOR_TEXT, font=ctk.CTkFont(family=FONT_FAMILY, size=12))
        combo.pack(fill="x", padx=18, pady=4)
        def save_device(device_kind, ip):
            if device_kind == 'sensor':
                with open(ESP_CONFIG, 'w', encoding='utf-8') as f:
                    f.write(ip)
                set_esp_ip(ip)
                self.update_label.configure(text=f"ESP: {ip}")
            else:
                endpoint = normalize_endpoint(ip)
                os.makedirs(os.path.dirname(self.fan_endpoint_file), exist_ok=True)
                with open(self.fan_endpoint_file, 'w', encoding='utf-8') as f:
                    f.write(endpoint)
                self.fan_endpoint = endpoint
                self._fan_generation += 1
                self.fan_status = None
                self.update_fan_dialog()
            refresh_current()
            status.configure(text="Адрес сохранён; соединение восстанавливается автоматически", text_color=COLOR_SUCCESS)
        def connect():
            ip = address.get().strip()
            expected = 'sensor' if kind.get() == 'Датчик АТС' else 'fan'
            if not ip:
                status.configure(text="Введите адрес", text_color=COLOR_WARNING)
                return
            status.configure(text="Проверяю тип устройства...", text_color=COLOR_MUTED)
            def worker():
                devices = probe_esp_devices(ip, timeout=1.5)
                def finish():
                    if not win.winfo_exists():
                        return
                    if (expected, ip) not in devices:
                        status.configure(text="Устройство этого типа не отвечает по указанному адресу", text_color=COLOR_WARNING)
                        return
                    try:
                        save_device(expected, ip)
                    except (OSError, ValueError) as exc:
                        status.configure(text=f"Не удалось сохранить адрес: {exc}", text_color=COLOR_WARNING)
                self.safe_after_from_worker(finish)
            threading.Thread(target=worker, daemon=True).start()
        ctk.CTkButton(row, text="Подключить", command=connect, width=100, height=32, corner_radius=8,
                      fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER).pack(side="right", padx=(8, 0))
        def select_found():
            device = found.get(choice.get())
            if device:
                device_kind, ip = device
                kind.set("Датчик АТС" if device_kind == 'sensor' else "Вентилятор")
                address.set(ip)
                connect()
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=12)
        scan_btn = ctk.CTkButton(buttons, text="Сканировать", width=120, height=32, corner_radius=8,
                                 fg_color=COLOR_CARD_ALT, hover_color=COLOR_BORDER, text_color=COLOR_TEXT,
                                 command=lambda: scan())
        scan_btn.pack(side="left")
        ctk.CTkButton(buttons, text="Подключить выбранное", command=select_found, height=32, corner_radius=8,
                      fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER).pack(side="right")
        def scan():
            stop_event.clear()
            found.clear()
            choice.set("")
            combo.configure(values=["Поиск..."])
            progress.set(0)
            scan_btn.configure(state="disabled")
            status.configure(text="Поиск датчика и вентилятора...", text_color=COLOR_MUTED)
            def progress_cb(done, total, ip, devices):
                def update():
                    if not win.winfo_exists():
                        return
                    progress.set(done / total if total else 1)
                    for device_kind, host in devices:
                        label = f"{'Датчик АТС' if device_kind == 'sensor' else 'Вентилятор'} — {host}"
                        found[label] = (device_kind, host)
                    if found:
                        combo.configure(values=list(found))
                        if not choice.get():
                            choice.set(next(iter(found)))
                    status.configure(text=f"Проверено {done}/{total}; найдено {len(found)}", text_color=COLOR_MUTED)
                self.safe_after_from_worker(update)
            def worker():
                scan_for_devices(progress_cb=progress_cb, stop_event=stop_event, fan_endpoint=self.fan_endpoint)
                def finish():
                    if win.winfo_exists():
                        scan_btn.configure(state="normal")
                        status.configure(text=f"Поиск завершён; найдено {len(found)}", text_color=COLOR_SUCCESS if found else COLOR_WARNING)
                self.safe_after_from_worker(finish)
            threading.Thread(target=worker, daemon=True).start()
        def close():
            stop_event.set()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)
        scan()

    def loop(self):
        global outside_temp, season_mode

        # Сетевые запросы выполняются в worker-потоках: главный поток только рисует UI.
        now = time.time()
        if not self._esp_refresh_running:
            self._esp_refresh_running = True

            def read_esp():
                endpoint = ESP_URL
                data = get_esp_data()
                completed_at = time.monotonic()
                def apply_esp():
                    if endpoint != ESP_URL:
                        self._esp_refresh_running = False
                        return
                    self._latest_esp = data
                    self._latest_esp_at = completed_at if data else self._latest_esp_at
                    self._esp_refresh_running = False
                self.safe_after_from_worker(apply_esp)
            threading.Thread(target=read_esp, daemon=True).start()

        fan_fresh = self._fan_last_poll_at and time.monotonic() - self._fan_last_poll_at <= 12
        outside_temp = outdoor_from_fan(self.fan_status if fan_fresh else None)
        season_mode = detect_season(outside_temp, season_mode)

        esp = validated_esp(self._latest_esp)
        if time.monotonic() - self._latest_esp_at > self.ESP_TIMEOUT:
            esp = None

        if esp:
            self.last_esp_seen = now
            was_offline = not self.esp_online
            self.esp_online = True

            t = float(esp.get("room_temp", 0))
            h = float(esp.get("humidity", 0))
            p = float(esp.get("pressure", 0))
            d = bool(esp.get("door", False))

            self.room_label.configure(
                text=f"Температура: {t:.1f} °C     Улица: {f'{outside_temp:.1f}' if outside_temp is not None else '--'} °C\n"
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
            if getattr(self, "_db_enabled", False) and now - self.last_logged >= self.LOG_INTERVAL:
                self.log_data_async(temp=t, hum=h, press=p, door=d, online=True)
                self.last_logged = now

            if was_offline:
                print("ESP снова ONLINE")

        else:
            check_alerts(None, None, None, "—")
            if self.esp_online and now - self.last_esp_seen > self.ESP_TIMEOUT:
                self.esp_online = False
                print("ESP OFFLINE")

                if getattr(self, "_db_enabled", False):
                    self.log_data_async(temp=None, hum=None, press=None, door=None, online=False)

            if not self.esp_online:
                outdoor_text = f"{outside_temp:.1f}" if outside_temp is not None else "--"
                self.room_label.configure(text=f"Температура: --.- °C     Улица: {outdoor_text} °C\nВлажность: -- %     Давление: ---- мм")
                self.door_state_label.configure(text="ДВЕРЬ: НЕТ ДАННЫХ", text_color=COLOR_MUTED)
                self.status_frame.configure(border_color=COLOR_BORDER)
                self.status_label.configure(text=f"ESP: OFFLINE\nРежим: {season_mode}", text_color=COLOR_DANGER)
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
        if not getattr(self, "_camera_updates_started", False):
            self._camera_updates_started = True
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
        else:
            self.cam_canvas.configure(image=None, text="Камера не подключена")
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

        # Native Tk uses physical pixels, matching winfo_width/height and PhotoImage.
        # CTkImage would apply DPI scaling a second time to these dimensions.
        from tkinter import Label
        cam_view = Label(win, text="", bg="black", borderwidth=0, highlightthickness=0)
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
                img = ImageTk.PhotoImage(Image.fromarray(frame))
                cam_view.camera_image = img
                cam_view.configure(image=img, text="")
            else:
                cam_view.configure(image="", text="Камера не подключена", fg="white")
                cam_view.camera_image = None
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
        history_generation = 0
        def load_data():
            nonlocal history_generation
            history_generation += 1
            generation = history_generation
            frm, to = date_from.get(), date_to.get()
            def worker():
                try:
                    conn = get_db_conn()
                    try:
                        with conn:
                            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                                cur.execute("""SELECT ts, temperature, humidity, door_open
                                    FROM esp_log WHERE ts BETWEEN %s AND %s ORDER BY ts""", (frm, to))
                                rows = cur.fetchall()
                    finally:
                        conn.close()
                except Exception as exc:
                    print("History error:", exc)
                    return
                def apply():
                    if win.winfo_exists() and generation == history_generation:
                        render_data(rows)
                self.safe_after_from_worker(apply)
            threading.Thread(target=worker, daemon=True).start()

        def render_data(rows):
            for item_id in table.get_children():
                table.delete(item_id)
            if not rows:
                ax.clear()
                ax.text(0.5, 0.5, "Нет данных за выбранный период", ha="center", va="center")
                canvas.draw()
                return

            times = [r["ts"] for r in rows]
            temps = [float(r["temperature"]) if r["temperature"] is not None else float("nan") for r in rows]
            hums = [float(r["humidity"]) if r["humidity"] is not None else float("nan") for r in rows]

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
                        f"{r['temperature']:.1f} °C" if r["temperature"] is not None else "—",
                        f"{r['humidity']:.0f} %" if r["humidity"] is not None else "—",
                        ("ОТКРЫТА" if door_open else "ЗАКРЫТА") if r["door_open"] is not None else "—",
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
            sound_file=os.path.join(BASE_DIR, "sounds", "alert.wav")
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

