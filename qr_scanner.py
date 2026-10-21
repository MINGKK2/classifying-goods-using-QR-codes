from __future__ import annotations

import csv
import base64
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cv2
import customtkinter as ctk
import numpy as np
import serial
from serial.tools import list_ports
from PIL import Image, ImageTk


APP_TITLE = "QR Vision Studio"
WINDOW_SIZE = "1440x900"
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720
MAX_HISTORY = 500
PROJECT_DIR = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_DIR / "qr_events.db"
ROUTES_PATH = PROJECT_DIR / "route_rules.json"
SETTINGS_PATH = PROJECT_DIR / "scanner_settings.json"


def load_scanner_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


SCANNER_SETTINGS = load_scanner_settings()
PASS_RELEASE_SECONDS = max(0.05, float(SCANNER_SETTINGS.get("pass_release_seconds", 0.45)))
PASS_STABLE_FRAMES = max(1, int(SCANNER_SETTINGS.get("stable_frames", 3)))

COLORS = {
    "window": "#070B14",
    "card": "#101725",
    "card_alt": "#0C1220",
    "border": "#202C40",
    "text": "#F4F7FB",
    "muted": "#91A0B8",
    "accent": "#2DD4BF",
    "accent_hover": "#14B8A6",
    "blue": "#60A5FA",
    "danger": "#FB7185",
    "warning": "#FBBF24",
}


@dataclass
class ScanRecord:
    event_id: int
    scanned_at: datetime
    content: str
    route: str
    mcu_status: str


@dataclass
class PresenceState:
    hits: int
    last_seen: float
    emitted: bool = False


class PassageGate:
    """Tạo một sự kiện cho mỗi lần QR xuất hiện rồi rời khỏi camera."""

    def __init__(
        self,
        release_after: float = PASS_RELEASE_SECONDS,
        stable_frames: int = PASS_STABLE_FRAMES,
    ) -> None:
        self.release_after = release_after
        self.stable_frames = stable_frames
        self._states: dict[str, PresenceState] = {}
        self._lock = threading.Lock()

    def update(self, visible_values: list[str], now: float) -> list[str]:
        ordered_visible = list(dict.fromkeys(value.strip() for value in visible_values if value.strip()))
        visible = set(ordered_visible)
        new_passes: list[str] = []
        with self._lock:
            for value in list(self._states):
                state = self._states[value]
                if value not in visible and now - state.last_seen >= self.release_after:
                    del self._states[value]

            for value in ordered_visible:
                state = self._states.get(value)
                if state is None:
                    state = PresenceState(hits=1, last_seen=now)
                    self._states[value] = state
                else:
                    state.hits += 1
                    state.last_seen = now

                if not state.emitted and state.hits >= self.stable_frames:
                    state.emitted = True
                    new_passes.append(value)

        return new_passes

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


class EventStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scanned_at TEXT NOT NULL,
                    qr_content TEXT NOT NULL,
                    route TEXT NOT NULL,
                    mcu_status TEXT NOT NULL,
                    mcu_message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.commit()

    def add(self, scanned_at: datetime, content: str, route: str, status: str) -> ScanRecord:
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_events (scanned_at, qr_content, route, mcu_status)
                VALUES (?, ?, ?, ?)
                """,
                (scanned_at.isoformat(timespec="milliseconds"), content, route, status),
            )
            event_id = int(cursor.lastrowid)
            connection.commit()
        return ScanRecord(event_id, scanned_at, content, route, status)

    def update_status(self, event_id: int, status: str, message: str = "") -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE scan_events SET mcu_status = ?, mcu_message = ? WHERE id = ?
                """,
                (status, message, event_id),
            )
            connection.commit()

    def recent(self, limit: int = MAX_HISTORY) -> list[ScanRecord]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, scanned_at, qr_content, route, mcu_status
                FROM scan_events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ScanRecord(
                int(row["id"]),
                datetime.fromisoformat(row["scanned_at"]),
                row["qr_content"],
                row["route"],
                row["mcu_status"],
            )
            for row in rows
        ]

    def all_records(self) -> list[ScanRecord]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, scanned_at, qr_content, route, mcu_status
                FROM scan_events ORDER BY id ASC
                """
            ).fetchall()
        return [
            ScanRecord(
                int(row["id"]),
                datetime.fromisoformat(row["scanned_at"]),
                row["qr_content"],
                row["route"],
                row["mcu_status"],
            )
            for row in rows
        ]


class SerialController:
    def __init__(self, event_queue: queue.Queue[tuple]) -> None:
        self._events = event_queue
        self._serial: serial.Serial | None = None
        self._read_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._ready = False

    @property
    def connected(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    @property
    def ready(self) -> bool:
        return self.connected and self._ready

    def connect(self, port: str, baudrate: int) -> None:
        self.disconnect()
        self._serial = serial.Serial(port=port, baudrate=baudrate, timeout=0.2, write_timeout=1)
        self._ready = False
        self._stop.clear()
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True, name="mcu-reader")
        self._read_thread.start()
        # MCU có thể đã khởi động trước khi PC mở COM; HELLO yêu cầu phát lại READY.
        try:
            self.send_command("HELLO", require_ready=False)
        except (serial.SerialException, OSError):
            self.disconnect()
            raise

    def disconnect(self) -> None:
        self._stop.set()
        self._ready = False
        serial_port = self._serial
        self._serial = None
        read_thread = self._read_thread
        self._read_thread = None
        if serial_port and serial_port.is_open:
            serial_port.close()
        if read_thread and read_thread.is_alive() and read_thread is not threading.current_thread():
            read_thread.join(timeout=0.5)

    def send_command(self, command: str, *arguments: object, require_ready: bool = True) -> None:
        if not self.connected or self._serial is None:
            raise serial.SerialException("Chưa kết nối MCU")
        if require_ready and not self.ready:
            raise serial.SerialException("MCU chưa gửi READY")

        fields = [command.upper(), *(str(argument) for argument in arguments)]
        payload = (",".join(fields) + "\n").encode("ascii")
        with self._write_lock:
            self._serial.write(payload)
            self._serial.flush()

    def send_queue(self, event_id: int, route: str, content: str) -> None:
        if not self.ready or self._serial is None:
            raise serial.SerialException("MCU chưa gửi READY")
        safe_route = re.sub(r"[^A-Za-z0-9_-]", "_", route)[:24] or "HOLD"
        # Base64 URL-safe giữ nguyên UTF-8, dấu phẩy và xuống dòng trong nội dung QR.
        raw_content = content.encode("utf-8")[:72]
        # Không cắt giữa một ký tự UTF-8; bản đầy đủ vẫn luôn được lưu trong SQLite.
        raw_content = raw_content.decode("utf-8", errors="ignore").encode("utf-8")
        qr_payload = base64.urlsafe_b64encode(raw_content).decode("ascii").rstrip("=")
        self.send_command("QUEUE", event_id, safe_route, qr_payload)

    def start_system(self) -> None:
        self.send_command("START")

    def stop_system(self) -> None:
        self.send_command("STOP", require_ready=False)

    def set_speed(self, percent: int) -> None:
        self.send_command("SPEED", max(0, min(100, int(percent))))

    def request_status(self) -> None:
        self.send_command("GET_STATUS")

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                serial_port = self._serial
                if serial_port is None or not serial_port.is_open:
                    return
                raw = serial_port.readline()
                if raw:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        if line.upper() == "READY":
                            self._ready = True
                        self._events.put(("mcu_line", line))
            except (serial.SerialException, OSError) as error:
                if not self._stop.is_set():
                    self._events.put(("mcu_error", str(error)))
                return

class QRScannerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.geometry(WINDOW_SIZE)
        self.minsize(1180, 760)
        self.configure(fg_color=COLORS["window"])
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._events: queue.Queue[tuple] = queue.Queue()
        self._frame_lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._camera_thread: threading.Thread | None = None
        self._stop_camera = threading.Event()
        self._passage_gate = PassageGate()
        self._store = EventStore(DATABASE_PATH)
        self._serial_controller = SerialController(self._events)
        self._pending_mcu: queue.Queue[ScanRecord] = queue.Queue()
        self._routes = self._load_routes()
        self._routes_mtime = ROUTES_PATH.stat().st_mtime if ROUTES_PATH.exists() else 0.0
        self._records: list[ScanRecord] = self._store.recent()
        self._last_content = ""
        self._photo: ImageTk.PhotoImage | None = None
        self._machine_state = "DISCONNECTED"
        self._motor_speed = 60
        self._fifo_capacity = "—"

        self._build_styles()
        self._build_ui()
        self._populate_history()
        self._refresh_serial_ports()
        self.after(30, self._poll_worker)

    @staticmethod
    def _load_routes() -> dict[str, str]:
        try:
            data = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in data.items()}
        except (OSError, json.JSONDecodeError, TypeError):
            return {"__DEFAULT__": "HOLD"}

    def _route_for(self, content: str) -> str:
        current_mtime = ROUTES_PATH.stat().st_mtime if ROUTES_PATH.exists() else 0.0
        if current_mtime != self._routes_mtime:
            self._routes = self._load_routes()
            self._routes_mtime = current_mtime
        return self._routes.get(content, self._routes.get("__DEFAULT__", "HOLD"))

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "QR.Treeview",
            background=COLORS["card_alt"],
            fieldbackground=COLORS["card_alt"],
            foreground=COLORS["text"],
            borderwidth=0,
            rowheight=34,
            font=("Segoe UI", 10),
        )
        style.map(
            "QR.Treeview",
            background=[("selected", "#164E63")],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "QR.Treeview.Heading",
            background="#162033",
            foreground=COLORS["muted"],
            borderwidth=0,
            relief="flat",
            font=("Segoe UI Semibold", 10),
        )
        style.map("QR.Treeview.Heading", background=[("active", "#1B2940")])

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=7)
        self.grid_columnconfigure(1, weight=4)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_camera_panel()
        self._build_data_panel()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent", height=82)
        header.grid(row=0, column=0, columnspan=2, padx=24, pady=(18, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_group,
            text="QR VISION",
            text_color=COLORS["text"],
            font=ctk.CTkFont(family="Segoe UI", size=25, weight="bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_group,
            text="Theo dõi thứ tự hàng • lưu SQLite • xếp lệnh FIFO cho MCU",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=13),
        ).pack(anchor="w", pady=(2, 0))

        self.status_label = ctk.CTkLabel(
            header,
            text="●  Camera đang tắt",
            width=155,
            height=34,
            corner_radius=17,
            fg_color="#1C2638",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
        )
        self.status_label.grid(row=0, column=2, padx=(10, 0), sticky="e")

        self.machine_status_pill = ctk.CTkLabel(
            header,
            text="◆  Dây chuyền offline",
            width=175,
            height=34,
            corner_radius=17,
            fg_color="#1C2638",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
        )
        self.machine_status_pill.grid(row=0, column=1, sticky="e")

    def _build_camera_panel(self) -> None:
        panel = ctk.CTkFrame(
            self,
            fg_color=COLORS["card"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=18,
        )
        panel.grid(row=1, column=0, padx=(24, 10), pady=(8, 24), sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(0, weight=1)

        self.video_label = ctk.CTkLabel(
            panel,
            text="CAMERA CHƯA KHỞI ĐỘNG\n\nNhấn “Bắt đầu” để quét mã QR",
            text_color=COLORS["muted"],
            fg_color="#060A12",
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=15),
        )
        self.video_label.grid(row=0, column=0, padx=14, pady=(14, 10), sticky="nsew")

        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.grid(row=1, column=0, padx=14, pady=(0, 14), sticky="ew")
        controls.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(
            controls,
            text="Nguồn camera",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
        ).grid(row=0, column=0, padx=(0, 8))

        self.camera_selector = ctk.CTkComboBox(
            controls,
            width=125,
            height=38,
            values=[f"Camera {index}" for index in range(5)],
            state="readonly",
            fg_color=COLORS["card_alt"],
            border_color=COLORS["border"],
            button_color="#1D4ED8",
            button_hover_color="#2563EB",
            dropdown_fg_color=COLORS["card"],
        )
        self.camera_selector.set("Camera 0")
        self.camera_selector.grid(row=0, column=1, padx=(0, 10))

        self.start_button = ctk.CTkButton(
            controls,
            text="▶  Bật camera",
            width=120,
            height=38,
            corner_radius=10,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color="#042F2E",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
            command=self._start_camera,
        )
        self.start_button.grid(row=0, column=2, padx=(0, 8))

        self.stop_button = ctk.CTkButton(
            controls,
            text="■  Tắt camera",
            width=95,
            height=38,
            corner_radius=10,
            fg_color="#2A3446",
            hover_color="#3B475C",
            text_color=COLORS["text"],
            state="disabled",
            command=self._request_stop_camera,
        )
        self.stop_button.grid(row=0, column=3)

        self.frame_info_label = ctk.CTkLabel(
            controls,
            text="Sẵn sàng",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self.frame_info_label.grid(row=0, column=4, padx=(16, 0), sticky="e")

        mcu_controls = ctk.CTkFrame(panel, fg_color="#0B1320", corner_radius=10)
        mcu_controls.grid(row=2, column=0, padx=14, pady=(0, 14), sticky="ew")
        mcu_controls.grid_columnconfigure(6, weight=1)

        ctk.CTkLabel(
            mcu_controls,
            text="MCU",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
        ).grid(row=0, column=0, padx=(12, 8), pady=9)

        self.port_selector = ctk.CTkComboBox(
            mcu_controls,
            width=110,
            height=34,
            values=["Không có cổng"],
            state="readonly",
            fg_color=COLORS["card_alt"],
            border_color=COLORS["border"],
        )
        self.port_selector.set("Không có cổng")
        self.port_selector.grid(row=0, column=1, padx=(0, 7), pady=9)

        self.baud_selector = ctk.CTkComboBox(
            mcu_controls,
            width=100,
            height=34,
            values=["9600", "57600", "115200", "230400"],
            state="readonly",
            fg_color=COLORS["card_alt"],
            border_color=COLORS["border"],
        )
        self.baud_selector.set("115200")
        self.baud_selector.grid(row=0, column=2, padx=(0, 7), pady=9)

        ctk.CTkButton(
            mcu_controls,
            text="↻",
            width=36,
            height=34,
            fg_color="#273449",
            hover_color="#364760",
            command=self._refresh_serial_ports,
        ).grid(row=0, column=3, padx=(0, 7), pady=9)

        self.mcu_button = ctk.CTkButton(
            mcu_controls,
            text="Kết nối",
            width=90,
            height=34,
            fg_color="#1D4ED8",
            hover_color="#2563EB",
            command=self._toggle_mcu_connection,
        )
        self.mcu_button.grid(row=0, column=4, padx=(0, 10), pady=9)

        self.mcu_status_label = ctk.CTkLabel(
            mcu_controls,
            text="● Chưa kết nối",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=12),
        )
        self.mcu_status_label.grid(row=0, column=6, padx=(0, 12), pady=9, sticky="e")

        machine_controls = ctk.CTkFrame(
            panel,
            fg_color="#0A1421",
            border_width=1,
            border_color="#1E344A",
            corner_radius=12,
        )
        machine_controls.grid(row=3, column=0, padx=14, pady=(0, 14), sticky="ew")
        machine_controls.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(
            machine_controls,
            text="ĐIỀU KHIỂN DÂY CHUYỀN",
            text_color="#7DD3FC",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
        ).grid(row=0, column=0, padx=(12, 12), pady=11)

        self.machine_start_button = ctk.CTkButton(
            machine_controls,
            text="▶ START",
            width=92,
            height=34,
            state="disabled",
            fg_color="#0F766E",
            hover_color="#0D9488",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            command=self._start_system,
        )
        self.machine_start_button.grid(row=0, column=1, padx=(0, 7), pady=9)

        self.machine_stop_button = ctk.CTkButton(
            machine_controls,
            text="■ STOP",
            width=88,
            height=34,
            state="disabled",
            fg_color="#BE123C",
            hover_color="#E11D48",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
            command=self._stop_system,
        )
        self.machine_stop_button.grid(row=0, column=2, padx=(0, 12), pady=9)

        self.speed_label = ctk.CTkLabel(
            machine_controls,
            text=f"PWM {self._motor_speed}%",
            width=70,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.speed_label.grid(row=0, column=3, padx=(0, 6))

        self.speed_slider = ctk.CTkSlider(
            machine_controls,
            from_=0,
            to=100,
            number_of_steps=20,
            width=125,
            state="disabled",
            progress_color=COLORS["accent"],
            button_color="#5EEAD4",
            button_hover_color="#99F6E4",
            command=self._on_speed_slider,
        )
        self.speed_slider.set(self._motor_speed)
        self.speed_slider.grid(row=0, column=4, padx=(0, 8))

        self.apply_speed_button = ctk.CTkButton(
            machine_controls,
            text="Gửi PWM",
            width=80,
            height=30,
            state="disabled",
            fg_color="#1E3A5F",
            hover_color="#254C78",
            command=self._apply_speed,
        )
        self.apply_speed_button.grid(row=0, column=5, padx=(0, 12), sticky="e")

    def _build_data_panel(self) -> None:
        panel = ctk.CTkFrame(
            self,
            fg_color=COLORS["card"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=18,
        )
        panel.grid(row=1, column=1, padx=(10, 24), pady=(8, 24), sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(5, weight=1)

        telemetry = ctk.CTkFrame(panel, fg_color="#0B1320", corner_radius=12)
        telemetry.grid(row=0, column=0, padx=16, pady=(16, 12), sticky="ew")
        telemetry.grid_columnconfigure((0, 1, 2), weight=1)

        self.sensor_value_label = self._telemetry_item(
            telemetry, 0, "CẢM BIẾN", "—", COLORS["blue"]
        )
        self.fifo_value_label = self._telemetry_item(
            telemetry, 1, "FIFO MCU", "0 / —", COLORS["accent"]
        )
        self.actuator_value_label = self._telemetry_item(
            telemetry, 2, "CƠ CẤU", "IDLE", COLORS["warning"]
        )

        ctk.CTkLabel(
            panel,
            text="NỘI DUNG VỪA QUÉT",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
        ).grid(row=1, column=0, padx=16, pady=(0, 7), sticky="w")

        self.last_content_box = ctk.CTkTextbox(
            panel,
            height=120,
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
            fg_color=COLORS["card_alt"],
            text_color=COLORS["text"],
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="word",
        )
        self.last_content_box.grid(row=2, column=0, padx=16, sticky="ew")
        self._set_last_content("Chưa có dữ liệu")

        history_header = ctk.CTkFrame(panel, fg_color="transparent")
        history_header.grid(row=4, column=0, padx=16, pady=(14, 7), sticky="ew")
        history_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            history_header,
            text="NHẬT KÝ PHÂN LOẠI",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
        ).grid(row=0, column=0, sticky="w")

        self.counter_label = ctk.CTkLabel(
            history_header,
            text="0 mã",
            width=60,
            height=25,
            corner_radius=12,
            fg_color="#15344A",
            text_color="#7DD3FC",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=11),
        )
        self.counter_label.grid(row=0, column=1, sticky="e")

        history_frame = ctk.CTkFrame(panel, fg_color=COLORS["card_alt"], corner_radius=12)
        history_frame.grid(row=5, column=0, padx=16, sticky="nsew")
        history_frame.grid_columnconfigure(0, weight=1)
        history_frame.grid_rowconfigure(0, weight=1)

        self.history_tree = ttk.Treeview(
            history_frame,
            columns=("id", "time", "content", "route", "status"),
            show="headings",
            style="QR.Treeview",
            selectmode="browse",
        )
        self.history_tree.heading("id", text="#")
        self.history_tree.heading("time", text="Giờ")
        self.history_tree.heading("content", text="Nội dung")
        self.history_tree.heading("route", text="Tuyến")
        self.history_tree.heading("status", text="MCU")
        self.history_tree.column("id", width=48, minwidth=42, anchor="center", stretch=False)
        self.history_tree.column("time", width=68, minwidth=64, anchor="center", stretch=False)
        self.history_tree.column("content", width=135, minwidth=90, anchor="w")
        self.history_tree.column("route", width=58, minwidth=52, anchor="center", stretch=False)
        self.history_tree.column("status", width=72, minwidth=62, anchor="center", stretch=False)
        self.history_tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        self.history_tree.bind("<<TreeviewSelect>>", self._show_selected_record)

        scrollbar = ttk.Scrollbar(history_frame, orient="vertical", command=self.history_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=8)
        self.history_tree.configure(yscrollcommand=scrollbar.set)

        buttons = ctk.CTkFrame(panel, fg_color="transparent")
        buttons.grid(row=6, column=0, padx=16, pady=(12, 8), sticky="ew")
        buttons.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkButton(
            buttons,
            text="Sao chép",
            height=36,
            corner_radius=9,
            fg_color="#1D4ED8",
            hover_color="#2563EB",
            command=self._copy_last_content,
        ).grid(row=0, column=0, padx=(0, 5), sticky="ew")

        ctk.CTkButton(
            buttons,
            text="Xuất CSV",
            height=36,
            corner_radius=9,
            fg_color="#374151",
            hover_color="#4B5563",
            command=self._export_csv,
        ).grid(row=0, column=1, padx=5, sticky="ew")

        ctk.CTkButton(
            buttons,
            text="Routes",
            height=36,
            corner_radius=9,
            fg_color="#3A2945",
            hover_color="#533763",
            text_color="#E9D5FF",
            command=self._open_route_rules,
        ).grid(row=0, column=2, padx=(5, 0), sticky="ew")

        ctk.CTkLabel(
            panel,
            text=f"Tự nhận kiện mới sau {PASS_RELEASE_SECONDS:.2f}s QR biến mất • mặc định: HOLD",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI", size=10),
        ).grid(row=7, column=0, padx=16, pady=(0, 5), sticky="w")

        ctk.CTkButton(
            panel,
            text="Xóa danh sách hiển thị (không xóa SQLite)",
            height=32,
            fg_color="transparent",
            hover_color="#2A1C27",
            text_color="#FDA4AF",
            command=self._clear_history,
        ).grid(row=8, column=0, padx=16, pady=(0, 14), sticky="ew")

    def _telemetry_item(
        self,
        parent: ctk.CTkFrame,
        column: int,
        title: str,
        value: str,
        color: str,
    ) -> ctk.CTkLabel:
        item = ctk.CTkFrame(parent, fg_color="transparent")
        item.grid(row=0, column=column, padx=8, pady=9, sticky="ew")
        ctk.CTkLabel(
            item,
            text=title,
            text_color=COLORS["muted"],
            font=ctk.CTkFont(family="Segoe UI Semibold", size=9),
        ).pack(anchor="center")
        value_label = ctk.CTkLabel(
            item,
            text=value,
            text_color=color,
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
        )
        value_label.pack(anchor="center", pady=(1, 0))
        return value_label

    def _refresh_serial_ports(self) -> None:
        ports = [port.device for port in list_ports.comports()]
        values = ports or ["Không có cổng"]
        self.port_selector.configure(values=values)
        if self.port_selector.get() not in values:
            self.port_selector.set(values[0])

    def _toggle_mcu_connection(self) -> None:
        if self._serial_controller.connected:
            try:
                self._serial_controller.stop_system()
            except (serial.SerialException, OSError):
                pass
            self._serial_controller.disconnect()
            self._discard_pending_mcu("MCU disconnected before READY")
            self.mcu_button.configure(text="Kết nối", fg_color="#1D4ED8")
            self.mcu_status_label.configure(text="● Chưa kết nối", text_color=COLORS["muted"])
            self.port_selector.configure(state="readonly")
            self.baud_selector.configure(state="readonly")
            self._set_machine_controls(False)
            self._set_machine_state("DISCONNECTED")
            return

        port = self.port_selector.get()
        if not port or port == "Không có cổng":
            messagebox.showwarning(APP_TITLE, "Không tìm thấy cổng COM của MCU.")
            return

        try:
            baudrate = int(self.baud_selector.get())
            self._serial_controller.connect(port, baudrate)
        except (ValueError, serial.SerialException, OSError) as error:
            messagebox.showerror(APP_TITLE, f"Không thể kết nối {port}:\n{error}")
            return

        self.mcu_button.configure(text="Ngắt kết nối", fg_color="#9F1239")
        self.mcu_status_label.configure(text=f"● {port} • chờ READY", text_color=COLORS["warning"])
        self.port_selector.configure(state="disabled")
        self.baud_selector.configure(state="disabled")

    def _set_machine_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.machine_start_button.configure(state=state)
        self.machine_stop_button.configure(state=state)
        self.speed_slider.configure(state=state)
        self.apply_speed_button.configure(state=state)

    def _set_machine_state(self, state: str) -> None:
        self._machine_state = state.upper()
        if self._machine_state == "RUNNING":
            text, color, background = "◆  Dây chuyền đang chạy", COLORS["accent"], "#123B36"
            self.machine_start_button.configure(state="disabled")
            self.machine_stop_button.configure(state="normal")
        elif self._machine_state == "STOPPED":
            text, color, background = "■  Dây chuyền đã dừng", COLORS["warning"], "#3F3310"
            if self._serial_controller.ready:
                self.machine_start_button.configure(state="normal")
                self.machine_stop_button.configure(state="normal")
        else:
            text, color, background = "◆  Dây chuyền offline", COLORS["muted"], "#1C2638"
        self.machine_status_pill.configure(text=text, text_color=color, fg_color=background)

    def _start_system(self) -> None:
        try:
            self._serial_controller.start_system()
        except (serial.SerialException, OSError) as error:
            messagebox.showerror(APP_TITLE, f"Không gửi được lệnh START:\n{error}")
            return
        self.machine_status_pill.configure(
            text="◆  Đang chờ START", text_color=COLORS["warning"], fg_color="#3F3310"
        )

    def _stop_system(self) -> None:
        try:
            self._serial_controller.stop_system()
        except (serial.SerialException, OSError) as error:
            messagebox.showerror(APP_TITLE, f"Không gửi được lệnh STOP:\n{error}")
            return
        self.machine_status_pill.configure(
            text="■  Đang chờ STOP", text_color=COLORS["warning"], fg_color="#3F3310"
        )

    def _on_speed_slider(self, value: float) -> None:
        self._motor_speed = int(round(value / 5) * 5)
        self.speed_label.configure(text=f"PWM {self._motor_speed}%")

    def _apply_speed(self) -> None:
        try:
            self._serial_controller.set_speed(self._motor_speed)
        except (serial.SerialException, OSError) as error:
            messagebox.showerror(APP_TITLE, f"Không gửi được tốc độ PWM:\n{error}")

    def _open_route_rules(self) -> None:
        try:
            if os.name == "nt":
                os.startfile(ROUTES_PATH)  # type: ignore[attr-defined]
            else:
                messagebox.showinfo(APP_TITLE, str(ROUTES_PATH))
        except OSError as error:
            messagebox.showerror(APP_TITLE, f"Không thể mở route_rules.json:\n{error}")

    def _start_camera(self) -> None:
        if self._camera_thread and self._camera_thread.is_alive():
            return

        try:
            camera_index = int(self.camera_selector.get().split()[-1])
        except (ValueError, IndexError):
            camera_index = 0

        self._stop_camera.clear()
        self._passage_gate.reset()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.camera_selector.configure(state="disabled")
        self._set_status("●  Đang kết nối", COLORS["warning"], "#3F3310")

        self._camera_thread = threading.Thread(
            target=self._camera_loop,
            args=(camera_index,),
            daemon=True,
            name="qr-camera-worker",
        )
        self._camera_thread.start()

    def _request_stop_camera(self) -> None:
        self._stop_camera.set()
        self.stop_button.configure(state="disabled")
        self._set_status("●  Đang dừng", COLORS["warning"], "#3F3310")

    def _open_camera(self, camera_index: int) -> cv2.VideoCapture:
        if os.name == "nt":
            capture = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
            if capture.isOpened():
                return capture
            capture.release()

        return cv2.VideoCapture(camera_index)

    def _camera_loop(self, camera_index: int) -> None:
        capture = self._open_camera(camera_index)
        if not capture.isOpened():
            self._events.put(("error", f"Không mở được Camera {camera_index}."))
            self._events.put(("stopped",))
            return

        capture.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        detector = cv2.QRCodeDetector()
        self._events.put(("running", camera_index))

        failed_reads = 0
        last_fps_time = time.perf_counter()
        frame_counter = 0
        scan_sequence = 0
        current_fps = 0.0

        try:
            while not self._stop_camera.is_set():
                ok, frame = capture.read()
                if not ok or frame is None:
                    failed_reads += 1
                    if failed_reads >= 20:
                        self._events.put(("error", "Camera không trả về hình ảnh."))
                        break
                    time.sleep(0.03)
                    continue

                failed_reads = 0
                scan_sequence += 1
                detected = self._decode_qr_codes(
                    detector,
                    frame,
                    use_enhanced=(scan_sequence % 5 == 0),
                )

                visible_values: list[str] = []
                for content, polygon in detected:
                    self._draw_detection(frame, polygon)
                    visible_values.append(content)

                new_passes = self._passage_gate.update(visible_values, time.monotonic())
                for content in new_passes:
                    scanned_at = datetime.now()
                    route = self._route_for(content)
                    if self._serial_controller.ready:
                        initial_status = "PENDING"
                    elif self._serial_controller.connected:
                        initial_status = "MCU_WAIT"
                    else:
                        initial_status = "OFFLINE"
                    record = self._store.add(scanned_at, content, route, initial_status)
                    # Đưa bản ghi vào UI trước khi gửi serial để ACK không thể đến trước dòng log.
                    self._events.put(("scan", record))

                    if self._serial_controller.ready:
                        try:
                            self._serial_controller.send_queue(record.event_id, route, content)
                            record.mcu_status = "SENT"
                            self._store.update_status(record.event_id, "SENT")
                            self._events.put(("record_status", record.event_id, "SENT"))
                        except (serial.SerialException, OSError) as error:
                            record.mcu_status = "ERROR"
                            self._store.update_status(record.event_id, "ERROR", str(error))
                            self._events.put(("record_status", record.event_id, "ERROR", str(error)))
                    elif self._serial_controller.connected:
                        self._pending_mcu.put(record)
                        if self._serial_controller.ready:
                            self._events.put(("flush_pending",))

                    # JSON Lines giúp chương trình khác đọc đúng thứ tự và nội dung.
                    print(
                        json.dumps(
                            {
                                "event_id": record.event_id,
                                "time": scanned_at.isoformat(timespec="milliseconds"),
                                "qr": content,
                                "route": route,
                                "mcu_status": record.mcu_status,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                frame_counter += 1
                now = time.perf_counter()
                elapsed = now - last_fps_time
                if elapsed >= 1.0:
                    current_fps = frame_counter / elapsed
                    frame_counter = 0
                    last_fps_time = now
                    self._events.put(("fps", current_fps, len(detected)))

                with self._frame_lock:
                    self._latest_frame = frame.copy()
        finally:
            capture.release()
            self._events.put(("stopped",))

    @staticmethod
    def _decode_qr_codes(
        detector: cv2.QRCodeDetector,
        frame: np.ndarray,
        use_enhanced: bool = True,
    ) -> list[tuple[str, np.ndarray]]:
        results: list[tuple[str, np.ndarray]] = []

        try:
            ok, decoded_values, points, _ = detector.detectAndDecodeMulti(frame)
            if ok and points is not None:
                for value, polygon in zip(decoded_values, points):
                    value = value.strip()
                    if value:
                        results.append((value, np.asarray(polygon, dtype=np.int32)))
        except (cv2.error, ValueError):
            pass

        if results:
            return results

        try:
            value, points, _ = detector.detectAndDecode(frame)
            value = value.strip()
            if value and points is not None:
                results.append((value, np.asarray(points, dtype=np.int32).reshape(-1, 2)))
        except (cv2.error, ValueError):
            pass

        if results or not use_enhanced:
            return results

        # Bản dự phòng cho QR nhỏ, ảnh giấy mờ hoặc camera hơi mất nét.
        # Chỉ chạy mỗi 5 frame để không làm giảm FPS quá nhiều.
        try:
            scale = 2.0
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            enlarged = cv2.resize(
                gray,
                None,
                fx=scale,
                fy=scale,
                interpolation=cv2.INTER_CUBIC,
            )
            _, binary = cv2.threshold(
                enlarged,
                0,
                255,
                cv2.THRESH_BINARY + cv2.THRESH_OTSU,
            )
            value, points, _ = detector.detectAndDecode(binary)
            value = value.strip()
            if value and points is not None:
                polygon = np.rint(
                    np.asarray(points, dtype=np.float32).reshape(-1, 2) / scale
                ).astype(np.int32)
                results.append((value, polygon))
        except (cv2.error, ValueError):
            pass

        return results

    @staticmethod
    def _draw_detection(frame: np.ndarray, polygon: np.ndarray) -> None:
        points = polygon.reshape((-1, 1, 2)).astype(np.int32)
        cv2.polylines(frame, [points], True, (191, 212, 45), 3, cv2.LINE_AA)

        x, y = polygon.reshape(-1, 2)[0]
        label_y = max(28, int(y) - 12)
        cv2.rectangle(frame, (int(x), label_y - 24), (int(x) + 105, label_y + 5), (20, 184, 166), -1)
        cv2.putText(
            frame,
            "QR DETECTED",
            (int(x) + 7, label_y - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (5, 30, 29),
            1,
            cv2.LINE_AA,
        )

    def _poll_worker(self) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._handle_worker_event(event)

        self._render_latest_frame()
        self.after(30, self._poll_worker)

    def _handle_worker_event(self, event: tuple) -> None:
        event_type = event[0]

        if event_type == "running":
            self._set_status("●  Đang quét", COLORS["accent"], "#123B36")
            self.frame_info_label.configure(text=f"Camera {event[1]} • đang theo dõi lượt hàng")
        elif event_type == "stopped":
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.camera_selector.configure(state="readonly")
            self._set_status("●  Camera đang tắt", COLORS["muted"], "#1C2638")
            self.frame_info_label.configure(text="Đã dừng")
            with self._frame_lock:
                self._latest_frame = None
        elif event_type == "error":
            self._set_status("●  Lỗi camera", COLORS["danger"], "#471C28")
            messagebox.showerror(APP_TITLE, event[1])
        elif event_type == "scan":
            self._add_record(event[1])
        elif event_type == "fps":
            fps, detected_count = event[1], event[2]
            self.frame_info_label.configure(
                text=f"{fps:.0f} FPS • {detected_count} mã trong khung"
            )
        elif event_type == "mcu_line":
            self._handle_mcu_line(event[1])
        elif event_type == "mcu_error":
            self._serial_controller.disconnect()
            self._discard_pending_mcu(event[1])
            self.mcu_button.configure(text="Kết nối", fg_color="#1D4ED8")
            self.mcu_status_label.configure(text="● Mất kết nối", text_color=COLORS["danger"])
            self.port_selector.configure(state="readonly")
            self.baud_selector.configure(state="readonly")
            self._set_machine_controls(False)
            self._set_machine_state("DISCONNECTED")
            self.sensor_value_label.configure(text="—", text_color=COLORS["muted"])
            self.fifo_value_label.configure(text="0 / —", text_color=COLORS["muted"])
            self.actuator_value_label.configure(text="IDLE", text_color=COLORS["muted"])
        elif event_type == "record_status":
            message = event[3] if len(event) > 3 else ""
            self._update_record_status(event[1], event[2], message)
        elif event_type == "flush_pending":
            self._flush_pending_mcu()

    def _handle_mcu_line(self, line: str) -> None:
        parts = [part.strip() for part in line.split(",")]
        command = parts[0].upper() if parts else ""

        if command == "READY":
            self.mcu_status_label.configure(text="● MCU sẵn sàng", text_color=COLORS["accent"])
            self._set_machine_controls(True)
            self._flush_pending_mcu()
            try:
                self._serial_controller.request_status()
            except (serial.SerialException, OSError):
                pass
            return

        if command in {"ACK", "DONE", "ERR"} and len(parts) >= 2:
            try:
                event_id = int(parts[1])
            except ValueError:
                return
            status = "ERROR" if command == "ERR" else command
            message = ",".join(parts[2:]) if len(parts) > 2 else line
            self._update_record_status(event_id, status, message)

        if command == "STATE" and len(parts) >= 2:
            self._set_machine_state(parts[1])
        elif command == "SENSOR" and len(parts) >= 2:
            active = parts[1].upper() in {"1", "ON", "ACTIVE", "BLOCKED"}
            self.sensor_value_label.configure(
                text="CÓ HÀNG" if active else "TRỐNG",
                text_color=COLORS["warning"] if active else COLORS["accent"],
            )
        elif command == "FIFO" and len(parts) >= 2:
            if len(parts) >= 3:
                self._fifo_capacity = parts[2]
            self.fifo_value_label.configure(
                text=f"{parts[1]} / {self._fifo_capacity}", text_color=COLORS["accent"]
            )
        elif command == "SPEED" and len(parts) >= 2:
            try:
                self._motor_speed = max(0, min(100, int(parts[1])))
                self.speed_slider.set(self._motor_speed)
                self.speed_label.configure(text=f"PWM {self._motor_speed}%")
            except ValueError:
                pass
        elif command == "ACTIVE" and len(parts) >= 2:
            route = parts[2] if len(parts) >= 3 else ""
            self.actuator_value_label.configure(
                text=f"#{parts[1]} {route}".strip(), text_color=COLORS["warning"]
            )
        elif command == "DONE" and len(parts) >= 2:
            self.actuator_value_label.configure(text="IDLE", text_color=COLORS["accent"])
        elif command == "HEARTBEAT" and len(parts) >= 6:
            self._set_machine_state(parts[2])
            active = parts[3] in {"1", "ON", "ACTIVE"}
            self.sensor_value_label.configure(
                text="CÓ HÀNG" if active else "TRỐNG",
                text_color=COLORS["warning"] if active else COLORS["accent"],
            )
            self.fifo_value_label.configure(
                text=f"{parts[4]} / {self._fifo_capacity}", text_color=COLORS["accent"]
            )
            try:
                self._motor_speed = max(0, min(100, int(parts[5])))
                self.speed_slider.set(self._motor_speed)
                self.speed_label.configure(text=f"PWM {self._motor_speed}%")
            except ValueError:
                pass
            self.mcu_status_label.configure(
                text=f"● UART ổn định • {parts[1]} ms", text_color=COLORS["accent"]
            )
            return

        self.mcu_status_label.configure(text=f"● RX: {line[:28]}", text_color=COLORS["blue"])

    def _flush_pending_mcu(self) -> None:
        while self._serial_controller.ready:
            try:
                record = self._pending_mcu.get_nowait()
            except queue.Empty:
                return
            try:
                self._serial_controller.send_queue(record.event_id, record.route, record.content)
                self._update_record_status(record.event_id, "SENT")
            except (serial.SerialException, OSError) as error:
                self._update_record_status(record.event_id, "ERROR", str(error))
                return

    def _discard_pending_mcu(self, reason: str) -> None:
        while True:
            try:
                record = self._pending_mcu.get_nowait()
            except queue.Empty:
                return
            self._update_record_status(record.event_id, "NOT_SENT", reason)

    def _update_record_status(self, event_id: int, status: str, message: str = "") -> None:
        self._store.update_status(event_id, status, message)
        for record in self._records:
            if record.event_id == event_id:
                record.mcu_status = status
                break
        item_id = f"event-{event_id}"
        if self.history_tree.exists(item_id):
            values = list(self.history_tree.item(item_id, "values"))
            if len(values) == 5:
                values[4] = status
                self.history_tree.item(item_id, values=values)

    def _render_latest_frame(self) -> None:
        with self._frame_lock:
            frame = None if self._latest_frame is None else self._latest_frame.copy()

        if frame is None:
            return

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)

        width = max(320, self.video_label.winfo_width())
        height = max(240, self.video_label.winfo_height())
        image.thumbnail((width, height), Image.Resampling.BILINEAR)

        canvas = Image.new("RGB", (width, height), (6, 10, 18))
        offset = ((width - image.width) // 2, (height - image.height) // 2)
        canvas.paste(image, offset)

        self._photo = ImageTk.PhotoImage(canvas)
        self.video_label.configure(image=self._photo, text="")

    def _populate_history(self) -> None:
        for record in reversed(self._records):
            self._insert_record(record, at_top=True)
        self.counter_label.configure(text=f"{len(self._records)} kiện")

    def _insert_record(self, record: ScanRecord, at_top: bool = True) -> None:
        preview = record.content.replace("\r", " ").replace("\n", " ")
        if len(preview) > 42:
            preview = preview[:39] + "..."

        item_id = f"event-{record.event_id}"
        if self.history_tree.exists(item_id):
            self.history_tree.delete(item_id)
        self.history_tree.insert(
            "",
            0 if at_top else "end",
            iid=item_id,
            values=(
                record.event_id,
                record.scanned_at.strftime("%H:%M:%S"),
                preview,
                record.route,
                record.mcu_status,
            ),
        )

    def _add_record(self, record: ScanRecord) -> None:
        self._records.insert(0, record)
        if len(self._records) > MAX_HISTORY:
            removed = self._records.pop()
            old_item = f"event-{removed.event_id}"
            if self.history_tree.exists(old_item):
                self.history_tree.delete(old_item)

        self._insert_record(record)
        children = self.history_tree.get_children()
        if len(children) > MAX_HISTORY:
            self.history_tree.delete(children[-1])

        self._last_content = record.content
        self._set_last_content(
            f"Sự kiện #{record.event_id}\n"
            f"Tuyến: {record.route}  |  MCU: {record.mcu_status}\n\n"
            f"{record.content}"
        )
        self.counter_label.configure(text=f"{len(self._records)} kiện")
        self._flash_scan_status()

    def _flash_scan_status(self) -> None:
        self._set_status("✓  Đã ghi sự kiện", "#A7F3D0", "#14532D")

        def restore() -> None:
            if self._camera_thread and self._camera_thread.is_alive():
                self._set_status("●  Đang quét", COLORS["accent"], "#123B36")

        self.after(1100, restore)

    def _set_status(self, text: str, text_color: str, background: str) -> None:
        self.status_label.configure(text=text, text_color=text_color, fg_color=background)

    def _set_last_content(self, content: str) -> None:
        self.last_content_box.configure(state="normal")
        self.last_content_box.delete("1.0", "end")
        self.last_content_box.insert("1.0", content)
        self.last_content_box.configure(state="disabled")

    def _show_selected_record(self, _event=None) -> None:
        selected = self.history_tree.selection()
        if not selected:
            return
        try:
            event_id = int(selected[0].removeprefix("event-"))
        except ValueError:
            return
        record = next((item for item in self._records if item.event_id == event_id), None)
        if record:
            self._last_content = record.content
            self._set_last_content(
                f"Sự kiện #{record.event_id}\n"
                f"Tuyến: {record.route}  |  MCU: {record.mcu_status}\n\n"
                f"{record.content}"
            )

    def _copy_last_content(self) -> None:
        if not self._last_content:
            messagebox.showinfo(APP_TITLE, "Chưa có nội dung QR để sao chép.")
            return

        self.clipboard_clear()
        self.clipboard_append(self._last_content)
        self.update_idletasks()
        self.frame_info_label.configure(text="Đã sao chép nội dung")

    def _clear_history(self) -> None:
        if not self._records:
            return
        if not messagebox.askyesno(APP_TITLE, "Xóa toàn bộ lịch sử quét trong giao diện?"):
            return

        self._records.clear()
        self.history_tree.delete(*self.history_tree.get_children())
        self._last_content = ""
        self._set_last_content("Chưa có dữ liệu")
        self.counter_label.configure(text="0 kiện")

    def _export_csv(self) -> None:
        records = self._store.all_records()
        if not records:
            messagebox.showinfo(APP_TITLE, "Chưa có lịch sử để xuất.")
            return

        default_name = f"qr_history_{datetime.now():%Y%m%d_%H%M%S}.csv"
        output_path = filedialog.asksaveasfilename(
            title="Xuất lịch sử QR",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV UTF-8", "*.csv")],
        )
        if not output_path:
            return

        try:
            with Path(output_path).open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(["STT", "Thời gian", "Nội dung QR", "Tuyến", "Trạng thái MCU"])
                for record in records:
                    writer.writerow(
                        [
                            record.event_id,
                            record.scanned_at.isoformat(sep=" ", timespec="milliseconds"),
                            record.content,
                            record.route,
                            record.mcu_status,
                        ]
                    )
        except OSError as error:
            messagebox.showerror(APP_TITLE, f"Không thể lưu file CSV:\n{error}")
            return

        messagebox.showinfo(APP_TITLE, f"Đã xuất {len(records)} sự kiện phân loại.")

    def _on_close(self) -> None:
        self._stop_camera.set()
        if self._serial_controller.connected:
            try:
                self._serial_controller.stop_system()
            except (serial.SerialException, OSError):
                pass
        self._serial_controller.disconnect()
        if self._camera_thread and self._camera_thread.is_alive():
            self._camera_thread.join(timeout=1.2)
        self.destroy()


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    app = QRScannerApp()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
