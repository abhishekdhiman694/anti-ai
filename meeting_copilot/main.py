"""Meeting Copilot - entry point.

Continuously listens to system audio (what's playing = the meeting) and
scans your screen on demand (via the "Scan Screen" button - no automatic
periodic capture). When a question is detected, shows a short, structured
best-approach answer.

Starts as a small floating bubble (bottom-right corner) that shows a
spinner while scanning and a checkmark when a fresh answer is ready.
Click it to expand into the full answer feed.

This window is a normal, visible desktop window - but IS excluded from
screen sharing/recording via SetWindowDisplayAffinity (see ui.py).

All "thinking" (transcription, vision, QA) happens on a backend server -
this client never talks to OpenAI directly and holds no API key. It signs
in with a username/password issued by whoever set up your access - that
username/password can only be exchanged for a session ONCE, ever - and the
server re-checks the resulting session (including expiry/revocation) on
every request, not just once at login - so access can be time-limited and
revoked centrally.

Run: double-click run.bat (or `python main.py` from an activated venv).
"""

import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

import keyboard
from dotenv import dotenv_values
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QFont
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QInputDialog, QLineEdit, QMessageBox,
)

import api_client
import config
from audio_capture import AudioCapture, MicRecorder, pcm_to_wav_bytes
from screen_capture import ScreenCapture
from ui import SidebarWindow


def _persisted_creds_path() -> Path:
    base = os.getenv("APPDATA") or str(Path.home())
    d = Path(base) / "MeetingCopilot"
    d.mkdir(parents=True, exist_ok=True)
    return d / ".env"


def _ensure_login() -> bool:
    """Signs in with a username/password issued by whoever set up your
    access. That username/password can only be exchanged for a session
    ONCE, ever - so this only succeeds on the very first launch after the
    credential was issued (or after the owner reactivates it). A saved
    username/password just saves retyping on that first launch; it doesn't
    grant a second login on a later relaunch. Returns False if the user
    cancels, the credential can't be used, or the server can't be reached."""
    persisted = _persisted_creds_path()
    saved = dotenv_values(persisted) if persisted.exists() else {}
    username, password = saved.get("MC_USERNAME"), saved.get("MC_PASSWORD")

    for _attempt in range(2):
        if not username or not password:
            username, ok1 = QInputDialog.getText(
                None, "Meeting Copilot - Sign in", "Username:"
            )
            if not ok1 or not username.strip():
                QMessageBox.critical(None, "Meeting Copilot", "A username is required.")
                return False
            password, ok2 = QInputDialog.getText(
                None, "Meeting Copilot - Sign in", "Password:", QLineEdit.Password
            )
            if not ok2 or not password:
                QMessageBox.critical(None, "Meeting Copilot", "A password is required.")
                return False
            username = username.strip()

        api_client.configure(config.SERVER_URL, username, password)
        try:
            expires_at = api_client.login()
        except api_client.AuthError as e:
            QMessageBox.warning(None, "Meeting Copilot", f"Sign-in failed: {e}. Try again.")
            username = password = None
            continue
        except RuntimeError as e:
            QMessageBox.critical(None, "Meeting Copilot", f"Could not reach server: {e}")
            return False

        persisted.write_text(
            f"MC_USERNAME={username}\nMC_PASSWORD={password}\n", encoding="utf-8"
        )
        print(f"[auth] signed in as {username!r}, access expires "
              f"{datetime.fromtimestamp(expires_at):%Y-%m-%d %H:%M}")
        return True

    return False


def format_answer(raw: str) -> str:
    return raw


def _describe_error(e: Exception) -> str:
    if isinstance(e, api_client.AuthError):
        return f"⚠ access expired or revoked: {e}"
    return f"⚠ server error: {e}"


def audio_worker(
    pcm_queue: "queue.Queue[bytes]",
    result_queue: "queue.Queue[tuple]",
    scanning_queue: "queue.Queue[bool]",
    status_queue: "queue.Queue[str]",
):
    while True:
        pcm = pcm_queue.get()
        print(f"[audio] captured utterance ({len(pcm)} bytes)")
        scanning_queue.put(True)
        try:
            answer = api_client.analyze_audio(pcm_to_wav_bytes(pcm), "audio")
            print(f"[audio] qa result: {answer!r}")
            if answer:
                result_queue.put(("audio", answer))
        except Exception as e:
            print(f"[audio] error: {e}")
            status_queue.put(_describe_error(e))
        finally:
            scanning_queue.put(False)


def screen_worker(
    result_queue: "queue.Queue[tuple]",
    scanning_queue: "queue.Queue[bool]",
    status_queue: "queue.Queue[str]",
):
    def on_text(text: str):
        print(f"[screen] extracted text: {text!r}")
        scanning_queue.put(True)
        try:
            answer = api_client.analyze_text(text, "screen")
            print(f"[screen] qa result: {answer!r}")
            if answer:
                result_queue.put(("screen", answer))
        except Exception as e:
            print(f"[screen] error: {e}")
            status_queue.put(_describe_error(e))
        finally:
            scanning_queue.put(False)

    def on_error(e: Exception):
        status_queue.put(_describe_error(e))

    capture = ScreenCapture(on_text=on_text, on_scanning=scanning_queue.put, on_error=on_error)
    capture.start()
    return capture


def create_tray_icon():
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setFont(QFont("Segoe UI Emoji", 20))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "🤖")
    painter.end()
    return QIcon(pixmap)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if not _ensure_login():
        sys.exit(1)

    window = SidebarWindow()
    # The application starts with the main window hidden

    pcm_queue: "queue.Queue[bytes]" = queue.Queue()
    result_queue: "queue.Queue[tuple]" = queue.Queue()
    status_queue: "queue.Queue[str]" = queue.Queue()
    scanning_queue: "queue.Queue[bool]" = queue.Queue()

    # Enumerate devices before opening any stream - pyaudiowpatch/WASAPI segfaults
    # if a second PyAudio host instance is created while a capture stream is open.
    available_devices = AudioCapture.list_loopback_devices()

    audio_capture = AudioCapture(out_queue=pcm_queue, on_status=status_queue.put)
    audio_capture.start()
    current_device_index = None  # device_index of whichever loopback capture is live

    threading.Thread(
        target=audio_worker,
        args=(pcm_queue, result_queue, scanning_queue, status_queue),
        daemon=True,
    ).start()
    screen_capture = screen_worker(result_queue, scanning_queue, status_queue)

    def start_loopback_capture(device_index):
        nonlocal audio_capture, current_device_index
        current_device_index = device_index
        audio_capture = AudioCapture(
            out_queue=pcm_queue, on_status=status_queue.put, device_index=device_index
        )
        audio_capture.start()

    def stop_loopback_capture():
        # Fully tear down the stream/PyAudio instance before opening a new one -
        # two open WASAPI streams (loopback or otherwise) at once crashes the process.
        audio_capture.stop()

    def set_mic_enabled(enabled: bool):
        audio_capture.set_enabled(enabled)

    def change_audio_device(device_index):
        print(f"[audio] switching capture device to index={device_index!r}")
        stop_loopback_capture()
        start_loopback_capture(device_index)

    def refresh_devices():
        current_name = None
        if window.device_combo.currentIndex() > 0:
            current_name = window.device_combo.currentText()

        print("[audio] refreshing device list and reconnecting...")
        status_queue.put("🔄 refreshing devices...")
        stop_loopback_capture()

        devices = AudioCapture.list_loopback_devices()
        window.set_device_list(devices, select_name=current_name)

        new_index = None
        if current_name is not None:
            new_index = next((d["index"] for d in devices if d["name"] == current_name), None)
            if new_index is None:
                status_queue.put(f"⚠ '{current_name}' no longer available, switched to Auto")

        start_loopback_capture(new_index)
        status_queue.put("listening • scanning")

    window.on_mic_toggle = set_mic_enabled
    window.on_screen_toggle = screen_capture.set_enabled
    window.on_scan_screen = screen_capture.scan_now
    window.on_device_change = change_audio_device
    window.on_device_refresh = refresh_devices
    window.set_device_list(available_devices)

    def on_search(query: str):
        scanning_queue.put(True)

        def worker():
            try:
                answer = api_client.answer_query(query)
                print(f"[search] qa result: {answer!r}")
                if answer:
                    result_queue.put(("search", answer))
            except Exception as e:
                print(f"[search] error: {e}")
                status_queue.put(_describe_error(e))
            finally:
                scanning_queue.put(False)

        threading.Thread(target=worker, daemon=True).start()

    window.on_search = on_search

    # Push-to-talk voice question: pyaudiowpatch can't have the loopback stream
    # and a real mic stream open at once (verified - it errors/crashes), so we
    # pause loopback capture for the few seconds of recording and resume it
    # right after, on whichever device was active before.
    mic_recorder = MicRecorder()

    def on_voice_ask_toggle(recording: bool):
        if recording:
            print("[mic] pausing loopback capture for voice question...")
            window.set_device_controls_enabled(False)
            stop_loopback_capture()
            try:
                mic_recorder.start()
            except Exception as e:
                print(f"[mic] failed to start recording: {e}")
                status_queue.put(f"⚠ mic error: {e}")
                start_loopback_capture(current_device_index)
                window.set_device_controls_enabled(True)
            return

        pcm = mic_recorder.stop()
        start_loopback_capture(current_device_index)
        window.set_device_controls_enabled(True)

        scanning_queue.put(True)

        def worker():
            try:
                if not pcm:
                    return
                text = api_client.transcribe(pcm)
                print(f"[mic] transcript: {text!r}")
                if not text or text.startswith("[transcription error"):
                    return
                answer = api_client.answer_query(text)
                print(f"[mic] qa result: {answer!r}")
                if answer:
                    result_queue.put(("voice", answer))
            except Exception as e:
                print(f"[mic] error: {e}")
                status_queue.put(_describe_error(e))
            finally:
                scanning_queue.put(False)

        threading.Thread(target=worker, daemon=True).start()

    window.on_voice_ask_toggle = on_voice_ask_toggle

    window.set_status("listening • scanning")

    # Periodic background re-check so an expired/revoked credential shows up
    # in the status bar even before you next try to use a feature - the
    # server already refuses every actual API call once expired, this is
    # just for earlier/passive visibility.
    def check_login_still_valid():
        def worker():
            try:
                api_client.check_session()
            except api_client.AuthError as e:
                status_queue.put(_describe_error(e))
            except RuntimeError:
                pass  # transient network hiccup - don't alarm over this

        threading.Thread(target=worker, daemon=True).start()

    login_check_timer = QTimer()
    login_check_timer.timeout.connect(check_login_still_valid)
    login_check_timer.start(3 * 60 * 1000)

    tray_icon = QSystemTrayIcon(create_tray_icon(), app)
    tray_menu = QMenu()

    show_action = tray_menu.addAction("Show Meeting Copilot")
    show_action.triggered.connect(window.show)

    hide_action = tray_menu.addAction("Hide Meeting Copilot")
    hide_action.triggered.connect(window.hide)

    quit_action = tray_menu.addAction("Quit")

    tray_icon.setContextMenu(tray_menu)
    tray_icon.show()

    # Use a Signal to safely jump from the background keyboard thread to the Qt main thread
    from PySide6.QtCore import QObject, Signal
    class GlobalSignals(QObject):
        toggle = Signal()

    global_signals = GlobalSignals()
    global_signals.toggle.connect(window.toggle_visibility)

    def on_tray_activated(reason):
        if reason == QSystemTrayIcon.DoubleClick:
            global_signals.toggle.emit()

    tray_icon.activated.connect(on_tray_activated)
    keyboard.add_hotkey('6+9+4', global_signals.toggle.emit)

    last_shown = {"audio": None, "screen": None}

    def poll_results():
        while not result_queue.empty():
            source, answer = result_queue.get()
            if source not in ("search", "voice"):
                normalized = answer.strip().casefold()
                if normalized == last_shown.get(source):
                    continue
                last_shown[source] = normalized
            window.add_entry(source, format_answer(answer))
        while not status_queue.empty():
            window.set_status(status_queue.get())
        while not scanning_queue.empty():
            window.set_scanning(scanning_queue.get())

    timer = QTimer()
    timer.timeout.connect(poll_results)
    timer.start(300)

    def on_quit():
        audio_capture.stop()
        screen_capture.stop()

    quit_action.triggered.connect(app.quit)
    app.aboutToQuit.connect(on_quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
