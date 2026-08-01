"""Visible, always-on-top glassmorphic widget with two states:

- Bubble (default): a small floating circle in the bottom-right corner.
  Shows a spinner while a scan (transcription/vision/analysis call) is in
  flight, and a checkmark badge when a fresh answer is ready but unseen.
- Panel (click to expand): the full feed of detected questions/answers.

This window is a normal on-screen window - it WILL appear in any screen
share, exactly like any other app."""

import ctypes
import sys

from PySide6.QtCore import Qt, QPoint, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QScreen, QFont
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QFrame,
    QPushButton,
    QLineEdit,
    QComboBox,
)

BUBBLE_SIZE = 60
PANEL_WIDTH = 380
PANEL_HEIGHT = 560
MARGIN = 24

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

GLASS_QSS = """
#glass {
    background-color: rgba(22, 22, 30, 150);
    border-radius: %RADIUS%px;
    border: 1px solid rgba(255, 255, 255, 35);
}
QLabel { color: #f0f0f5; background: transparent; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: rgba(255,255,255,60); border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
"""

CARD_QSS = """
QFrame {
    background-color: rgba(255, 255, 255, 22);
    border-radius: 12px;
    border: 1px solid rgba(255, 255, 255, 40);
}
QLabel { color: #f0f0f5; background: transparent; }
"""


def _try_enable_windows_acrylic(hwnd: int) -> bool:
    """Best-effort true OS-level blur-behind (Windows 10/11 Acrylic).
    Falls back silently to flat translucency if unsupported."""
    try:
        class ACCENTPOLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_int),
                ("AnimationId", ctypes.c_int),
            ]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.POINTER(ACCENTPOLICY)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        accent = ACCENTPOLICY()
        accent.AccentState = 4          # ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = 2
        accent.GradientColor = 0x99261E1E  # AABBGGRR - dark tint, ~60% alpha
        accent.AnimationId = 0

        data = WINCOMPATTRDATA()
        data.Attribute = 19             # WCA_ACCENT_POLICY
        data.SizeOfData = ctypes.sizeof(accent)
        data.Data = ctypes.pointer(accent)

        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.pointer(data))
        return True
    except Exception:
        return False


class AnswerCard(QFrame):
    def __init__(self, source: str, text: str):
        super().__init__()
        self.setStyleSheet(CARD_QSS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        if source == "audio":
            icon = "\U0001F3A4"
        elif source == "search":
            icon = "\U0001F50D"
        elif source == "voice":
            icon = "\U0001F399"
        else:
            icon = "\U0001F5A5"
        tag = QLabel(f"{icon} {source.upper()}")
        tag.setStyleSheet("color:#9db8ff; font-weight:600; font-size:11px;")
        layout.addWidget(tag)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        mono = QFont("Consolas")
        mono.setPointSize(10)
        body.setFont(mono)
        layout.addWidget(body)


class SidebarWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Meeting Copilot")
        self.setWindowFlags(Qt.Window | Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._drag_pos: QPoint | None = None
        self._expanded = False
        self._scanning = 0
        self._unread = False
        self._spinner_frame = 0
        self.on_mic_toggle = None      # set by main.py: callable(bool)
        self.on_screen_toggle = None   # set by main.py: callable(bool)
        self.on_search = None         # set by main.py: callable(str)
        self.on_device_change = None  # set by main.py: callable(int | None)
        self.on_device_refresh = None  # set by main.py: callable()
        self.on_voice_ask_toggle = None  # set by main.py: callable(bool)
        self.on_scan_screen = None    # set by main.py: callable()
        self._populating_devices = False

        self._build_ui()
        self._collapse(animate=False)

        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        self._spinner_timer.start(90)

        # Belt-and-suspenders "always on top": re-asserting HWND_TOPMOST only
        # at click time (in _collapse/_expand) wasn't enough - anything else
        # that knocks the window out of the topmost z-band between clicks
        # (another app coming to focus, etc.) left it stuck behind until the
        # next click. This self-heals continuously instead.
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._reassert_topmost)
        self._topmost_timer.start(250)

    def showEvent(self, event):
        super().showEvent(event)
        hwnd = int(self.winId())
        _try_enable_windows_acrylic(hwnd)
        self._reassert_topmost(_framechanged=True)
        # Ensure the API is called after the window is fully created and shown
        QTimer.singleShot(0, lambda: self._enable_screen_capture_protection(hwnd))

    def _enable_screen_capture_protection(self, hwnd: int):
        if sys.platform != "win32":
            return
            
        if not hwnd:
            print("Error: Invalid HWND obtained for screen capture protection.")
            return

        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        try:
            # We use WinDLL with use_last_error=True to reliably capture the thread's last error
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            SetWindowDisplayAffinity = user32.SetWindowDisplayAffinity
            # Set the exact C signatures for the API
            SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            SetWindowDisplayAffinity.restype = ctypes.c_bool

            # The application is already a top-level native window due to Qt.Window and Qt.FramelessWindowHint
            success = SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if success:
                print(f"Screen capture protection successfully enabled for window {hwnd}.")
            else:
                error_code = ctypes.get_last_error()
                error_message = ctypes.FormatError(error_code)
                print(f"Failed to set window display affinity. Error code: {error_code}, Message: {error_message}")
        except Exception as e:
            print(f"Exception setting window display affinity: {e}")

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def toggle_visibility(self):
        if self.isHidden():
            self.show()
            # If not expanded, make sure we show in bubble mode on right edge
            if not self._expanded:
                self._collapse(animate=False)
        else:
            self.hide()

    # ---------- UI construction ----------

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.glass = QFrame()
        self.glass.setObjectName("glass")
        outer.addWidget(self.glass)

        stack = QVBoxLayout(self.glass)
        stack.setContentsMargins(0, 0, 0, 0)

        # --- bubble content ---
        self.bubble_widget = QWidget()
        bl = QVBoxLayout(self.bubble_widget)
        bl.setContentsMargins(0, 0, 0, 0)
        self.bubble_label = QLabel("🤖")
        self.bubble_label.setAlignment(Qt.AlignCenter)
        self.bubble_label.setStyleSheet("font-size: 24px;")
        bl.addWidget(self.bubble_label)
        stack.addWidget(self.bubble_widget)

        # --- panel content ---
        self.panel_widget = QWidget()
        root = QVBoxLayout(self.panel_widget)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Meeting Copilot")
        title.setStyleSheet("color:white; font-size:15px; font-weight:700;")
        header.addWidget(title)
        header.addStretch()
        self.status_label = QLabel("starting...")
        self.status_label.setStyleSheet("color:#c8c8d8; font-size:11px;")
        header.addWidget(self.status_label)

        collapse_btn = QPushButton("—")
        collapse_btn.setFixedSize(22, 22)
        collapse_btn.setStyleSheet(
            "QPushButton { background:rgba(255,255,255,25); color:#eee; border-radius:11px; }"
            "QPushButton:hover { background:rgba(255,255,255,50); }"
        )
        collapse_btn.clicked.connect(lambda: self._collapse(animate=True))
        header.addWidget(collapse_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(22, 22)
        close_btn.setStyleSheet(
            "QPushButton { background:rgba(255,255,255,25); color:#eee; border-radius:11px; }"
            "QPushButton:hover { background:rgba(255,80,80,140); }"
        )
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn)
        root.addLayout(header)

        toolbar = QHBoxLayout()

        self.mic_btn = QPushButton("🎤 Mic: ON")
        self.mic_btn.setCheckable(True)
        self.mic_btn.setChecked(True)
        self.mic_btn.setStyleSheet(self._toggle_qss())
        self.mic_btn.clicked.connect(self._on_mic_toggled)
        toolbar.addWidget(self.mic_btn)

        self.screen_btn = QPushButton("🖥 Screen: ON")
        self.screen_btn.setCheckable(True)
        self.screen_btn.setChecked(True)
        self.screen_btn.setStyleSheet(self._toggle_qss())
        self.screen_btn.clicked.connect(self._on_screen_toggled)
        toolbar.addWidget(self.screen_btn)

        scan_btn = QPushButton("🔍 Scan Screen")
        scan_btn.setToolTip("Scan your screen right now, regardless of the automatic timer")
        scan_btn.setStyleSheet(
            "QPushButton { background:rgba(157,184,255,60); color:#eef2ff; border-radius:6px; "
            "padding:4px 10px; border:1px solid rgba(157,184,255,110); }"
            "QPushButton:hover { background:rgba(157,184,255,100); }"
        )
        scan_btn.clicked.connect(self._on_scan_screen_clicked)
        toolbar.addWidget(scan_btn)

        toolbar.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.setStyleSheet(
            "QPushButton { background:rgba(255,255,255,25); color:#ddd; border-radius:6px; padding:4px 10px; }"
            "QPushButton:hover { background:rgba(255,255,255,45); }"
        )
        clear_btn.clicked.connect(self._clear)
        toolbar.addWidget(clear_btn)

        root.addLayout(toolbar)

        device_row = QHBoxLayout()
        device_label = QLabel("🔊 Listen to:")
        device_label.setStyleSheet("color:#c8c8d8; font-size:11px;")
        device_row.addWidget(device_label)

        self.device_combo = QComboBox()
        self.device_combo.setStyleSheet(
            "QComboBox { background:rgba(255,255,255,25); color:#f0f0f5; border-radius:6px; "
            "padding:3px 8px; border:1px solid rgba(255,255,255,40); }"
            "QComboBox QAbstractItemView { background:#20202a; color:#f0f0f5; "
            "selection-background-color:rgba(157,184,255,90); }"
        )
        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        device_row.addWidget(self.device_combo, stretch=1)

        self.refresh_btn = QPushButton("🔄")
        self.refresh_btn.setFixedSize(26, 26)
        self.refresh_btn.setToolTip("Rescan audio devices and reconnect")
        self.refresh_btn.setStyleSheet(
            "QPushButton { background:rgba(255,255,255,25); color:#eee; border-radius:6px; }"
            "QPushButton:hover { background:rgba(255,255,255,50); }"
        )
        self.refresh_btn.clicked.connect(self._on_refresh_clicked)
        device_row.addWidget(self.refresh_btn)

        root.addLayout(device_row)

        search_row = QHBoxLayout()

        self.voice_btn = QPushButton("🎙️")
        self.voice_btn.setCheckable(True)
        self.voice_btn.setFixedSize(34, 30)
        self.voice_btn.setToolTip("Click to ask a question by voice, click again to stop and get an answer")
        self.voice_btn.setStyleSheet(
            "QPushButton { background:rgba(255,255,255,25); color:#eee; border-radius:6px; font-size:14px; }"
            "QPushButton:hover { background:rgba(255,255,255,50); }"
            "QPushButton:checked { background:rgba(255,90,90,150); color:white; }"
        )
        self.voice_btn.clicked.connect(self._on_voice_btn_clicked)
        search_row.addWidget(self.voice_btn)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Ask anything...")
        self.search_input.setStyleSheet(
            "QLineEdit { background:rgba(255,255,255,25); color:#f0f0f5; "
            "border-radius:6px; padding:5px 10px; border:1px solid rgba(255,255,255,40); }"
            "QLineEdit:focus { border:1px solid rgba(157,184,255,160); }"
        )
        self.search_input.returnPressed.connect(self._on_search_submitted)
        search_row.addWidget(self.search_input)

        search_btn = QPushButton("Ask")
        search_btn.setStyleSheet(
            "QPushButton { background:rgba(157,184,255,60); color:#eef2ff; border-radius:6px; "
            "padding:5px 12px; border:1px solid rgba(157,184,255,110); }"
            "QPushButton:hover { background:rgba(157,184,255,100); }"
        )
        search_btn.clicked.connect(self._on_search_submitted)
        search_row.addWidget(search_btn)

        root.addLayout(search_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.feed_widget = QWidget()
        self.feed_layout = QVBoxLayout(self.feed_widget)
        self.feed_layout.setSpacing(8)
        self.feed_layout.addStretch()
        self.scroll.setWidget(self.feed_widget)
        root.addWidget(self.scroll)

        stack.addWidget(self.panel_widget)
        self._stack_layout = stack

    # ---------- state transitions ----------

    def _apply_glass_style(self, radius: int):
        self.glass.setStyleSheet(GLASS_QSS.replace("%RADIUS%", str(radius)))

    def _bottom_right_geometry(self, w: int, h: int):
        screen: QScreen = QApplication.primaryScreen()
        geo = screen.availableGeometry()
        return geo.right() - w - MARGIN, geo.bottom() - h - MARGIN, w, h

    def _collapse(self, animate: bool):
        self._expanded = False
        self.panel_widget.setVisible(False)
        self.bubble_widget.setVisible(True)
        self._apply_glass_style(BUBBLE_SIZE // 2)
        x, y, w, h = self._bottom_right_geometry(BUBBLE_SIZE, BUBBLE_SIZE)
        self._animate_or_set(x, y, w, h, animate)
        self._set_noactivate(True)

    def _expand(self, animate: bool):
        self._expanded = True
        self._unread = False
        self.bubble_widget.setVisible(False)
        self.panel_widget.setVisible(True)
        self._apply_glass_style(18)
        x, y, w, h = self._bottom_right_geometry(PANEL_WIDTH, PANEL_HEIGHT)
        self._animate_or_set(x, y, w, h, animate)
        self._set_noactivate(False)

    def _set_noactivate(self, enable: bool):
        """Bubble mode should feel like a real overlay: appearing (e.g. via
        the global hotkey) must never steal focus from whatever app you're
        using. Panel mode needs normal focus/activation so you can actually
        click into the search box and type."""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = ctypes.c_bool

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_NOACTIVATE) if enable else (style & ~WS_EX_NOACTIVATE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

            # Changing EXSTYLE at runtime can silently knock a window out of
            # the "always on top" z-band even though WS_EX_TOPMOST stays set -
            # re-assert it immediately too (on top of the periodic timer).
            self._reassert_topmost(_framechanged=True)
        except Exception as e:
            print(f"[ui] failed to toggle no-activate style: {e}")

    def _reassert_topmost(self, _framechanged: bool = False):
        if sys.platform != "win32" or self.isHidden():
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, ctypes.c_uint,
            ]
            user32.SetWindowPos.restype = ctypes.c_bool
            HWND_TOPMOST = ctypes.c_void_p(-1)
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE_FLAG, SWP_FRAMECHANGED = 0x2, 0x1, 0x10, 0x20
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE_FLAG
            if _framechanged:
                flags |= SWP_FRAMECHANGED
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        except Exception:
            pass

    def _animate_or_set(self, x, y, w, h, animate: bool):
        if not animate:
            self.setGeometry(x, y, w, h)
            return
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(180)
        anim.setStartValue(self.geometry())
        anim.setEndValue(self.geometry().__class__(x, y, w, h))
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        self._anim = anim  # keep a reference alive

    def _position_right_edge(self):
        self._collapse(animate=False)

    # ---------- public API used by main.py ----------

    def toggle(self):
        if self._expanded:
            self._collapse(animate=True)
        else:
            self._expand(animate=True)

    def _clear(self):
        while self.feed_layout.count() > 1:
            item = self.feed_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _toggle_qss() -> str:
        return """
        QPushButton {
            background: rgba(120, 200, 140, 60);
            color: #ddffe4;
            border-radius: 6px;
            padding: 4px 10px;
            border: 1px solid rgba(120, 200, 140, 90);
        }
        QPushButton:hover { background: rgba(120, 200, 140, 90); }
        QPushButton:!checked {
            background: rgba(255, 255, 255, 20);
            color: #999;
            border: 1px solid rgba(255, 255, 255, 35);
        }
        QPushButton:!checked:hover { background: rgba(255,255,255,40); }
        """

    def _on_mic_toggled(self, checked: bool):
        self.mic_btn.setText(f"🎤 Mic: {'ON' if checked else 'OFF'}")
        if self.on_mic_toggle:
            self.on_mic_toggle(checked)

    def _on_screen_toggled(self, checked: bool):
        self.screen_btn.setText(f"🖥 Screen: {'ON' if checked else 'OFF'}")
        if self.on_screen_toggle:
            self.on_screen_toggle(checked)

    def _on_scan_screen_clicked(self):
        self.set_status("🔍 scanning your screen...")
        if self.on_scan_screen:
            self.on_scan_screen()

    def set_device_list(self, devices: list[dict], select_name: str | None = None):
        """devices: [{'index': int, 'name': str}, ...]. select_name re-selects a
        device by display name after a refresh (falls back to Auto if not found)."""
        self._populating_devices = True
        self.device_combo.clear()
        self.device_combo.addItem("Auto (system default)", userData=None)
        match_idx = 0
        for device in devices:
            self.device_combo.addItem(device["name"], userData=device["index"])
            if select_name is not None and device["name"] == select_name:
                match_idx = self.device_combo.count() - 1
        self.device_combo.setCurrentIndex(match_idx)
        self._populating_devices = False

    def _on_device_selected(self, idx: int):
        if self._populating_devices or idx < 0:
            return
        device_index = self.device_combo.itemData(idx)
        if self.on_device_change:
            self.on_device_change(device_index)

    def _on_refresh_clicked(self):
        if self.on_device_refresh:
            self.on_device_refresh()

    def set_device_controls_enabled(self, enabled: bool):
        """Disabled while a voice question is recording, since switching/refreshing
        the loopback device at that moment would race with the paused capture."""
        self.device_combo.setEnabled(enabled)
        self.refresh_btn.setEnabled(enabled)

    def _on_voice_btn_clicked(self, checked: bool):
        if checked:
            self.voice_btn.setText("⏹")
            self.voice_btn.setToolTip("Recording... click to stop and get an answer")
            self.set_status("🎙️ listening to your question...")
        else:
            self.voice_btn.setText("🎙️")
            self.voice_btn.setToolTip("Click to ask a question by voice, click again to stop and get an answer")
        if self.on_voice_ask_toggle:
            self.on_voice_ask_toggle(checked)

    def _on_search_submitted(self):
        query = self.search_input.text().strip()
        if not query:
            return
        self.search_input.clear()
        if self.on_search:
            self.on_search(query)

    def add_entry(self, source: str, text: str):
        card = AnswerCard(source, text)
        self.feed_layout.insertWidget(self.feed_layout.count() - 1, card)
        self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())
        if not self._expanded:
            self._unread = True
        self._refresh_bubble_icon()

    def set_status(self, text: str):
        self.status_label.setText(text)

    def set_scanning(self, is_scanning: bool):
        self._scanning += 1 if is_scanning else -1
        if self._scanning < 0:
            self._scanning = 0
        self._refresh_bubble_icon()

    def _tick_spinner(self):
        if self._scanning > 0 and not self._expanded:
            self._spinner_frame = (self._spinner_frame + 1) % len(SPINNER_FRAMES)
            self._refresh_bubble_icon()

    def _refresh_bubble_icon(self):
        if self._scanning > 0:
            self.bubble_label.setText(SPINNER_FRAMES[self._spinner_frame])
            self.bubble_label.setStyleSheet("font-size: 26px; color: #9db8ff;")
        elif self._unread:
            self.bubble_label.setText("✅")
            self.bubble_label.setStyleSheet("font-size: 24px;")
        else:
            self.bubble_label.setText("🤖")
            self.bubble_label.setStyleSheet("font-size: 24px;")

    # ---------- drag-to-move + click-to-toggle ----------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False
            self._press_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            if (event.globalPosition().toPoint() - self._press_pos).manhattanLength() > 6:
                self._dragged = True
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        was_dragged = getattr(self, "_dragged", False)
        self._drag_pos = None
        if not was_dragged and not self._expanded:
            self.toggle()
        elif not was_dragged and self._expanded and event.position().y() < 40:
            pass  # click on header while expanded - handled by buttons
