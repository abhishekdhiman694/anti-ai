"""Screenshots the primary display on demand (manual scan only - no automatic
periodic/change-triggered capture) and sends it to the backend server for
vision extraction (pull out any visible question / problem statement). The
client never talks to OpenAI directly - see api_client.py."""

import io
import threading

import mss
from PIL import Image

import api_client


class ScreenCapture:
    def __init__(self, on_text, on_scanning=None, on_error=None):
        """on_text: callable(str) invoked with extracted question text (already
        filtered for NONE). on_scanning: callable(bool) invoked True right
        before a vision call starts, False right after it ends. on_error:
        callable(Exception) invoked on failure (e.g. AuthError if your
        access has expired)."""
        self.on_text = on_text
        self.on_scanning = on_scanning or (lambda v: None)
        self.on_error = on_error or (lambda e: None)
        self._enabled = threading.Event()
        self._enabled.set()

    def start(self):
        pass  # no background loop - scans only happen via scan_now()

    def stop(self):
        pass

    def set_enabled(self, enabled: bool):
        """Gates scan_now() - lets you fully disable screen capture (e.g.
        before sharing your screen) even though scanning is manual-only."""
        if enabled:
            self._enabled.set()
        else:
            self._enabled.clear()

    def scan_now(self):
        """Manually triggered scan - the only way a screenshot is ever taken.
        No automatic/periodic/change-triggered capture happens on its own."""
        if not self._enabled.is_set():
            print("[screen] scan blocked - screen capture is toggled OFF")
            return
        threading.Thread(target=self._scan_once, daemon=True).start()

    def _scan_once(self):
        try:
            img = self._grab()
            print("[screen] manual scan triggered")
            self.on_scanning(True)
            try:
                text = self._extract(img)
            finally:
                self.on_scanning(False)
            if text:
                self.on_text(text)
            else:
                print("[screen] manual scan: nothing relevant found")
        except Exception as e:
            print(f"[screen] manual scan error: {e}")
            self.on_error(e)

    def _grab(self) -> Image.Image:
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary display
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            return img

    def _extract(self, img: Image.Image) -> str | None:
        img = img.copy()
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return api_client.extract_screen(buf.getvalue())
