"""Captures system (loopback) audio - i.e. what's playing through your speakers,
which is the other participants' voice in a Meet/Zoom/Teams call - and cuts it
into utterances (speech segments separated by silence) for transcription."""

import queue
import struct
import threading
import time

import numpy as np
import pyaudiowpatch as pyaudio

import config


def _rms(frame: bytes) -> float:
    data = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    if data.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(data ** 2)))


class AudioCapture:
    """Runs in a background thread. Pushes finished utterances (raw PCM bytes,
    16kHz mono 16-bit) onto `out_queue`."""

    def __init__(self, out_queue: "queue.Queue[bytes]", on_status=None, device_index: int | None = None):
        self.out_queue = out_queue
        self.on_status = on_status or (lambda text: None)
        self.device_index = device_index
        self._stop = threading.Event()
        self._enabled = threading.Event()
        self._enabled.set()
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._last_read_ts = time.monotonic()
        self._lock = threading.Lock()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._watchdog_thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def set_enabled(self, enabled: bool):
        if enabled:
            self._enabled.set()
        else:
            self._enabled.clear()

    def _watchdog(self):
        warned = False
        # give the stream a few seconds to open before judging silence
        time.sleep(5)
        while not self._stop.is_set():
            if not self._enabled.is_set():
                time.sleep(2)
                continue
            with self._lock:
                idle_for = time.monotonic() - self._last_read_ts
            if idle_for > 6 and not warned:
                warned = True
                self.on_status(
                    "⚠ no audio detected - if your output device is "
                    "Bluetooth, switch to speakers/wired and restart"
                )
            elif idle_for <= 6 and warned:
                warned = False
                self.on_status("listening • scanning")
            time.sleep(2)

    def _find_loopback_device(self, pa: pyaudio.PyAudio):
        if self.device_index is not None:
            try:
                device = pa.get_device_info_by_index(self.device_index)
                print(f"[audio] using manually selected device: {device['name']!r}")
                return device
            except Exception as e:
                print(f"[audio] selected device index {self.device_index} unavailable ({e}), "
                      f"falling back to default output device")

        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        if not default_speakers.get("isLoopbackDevice", False):
            for device in pa.get_loopback_device_info_generator():
                if default_speakers["name"] in device["name"]:
                    return device
            raise RuntimeError("Could not find a loopback device for default output.")
        return default_speakers

    @staticmethod
    def list_loopback_devices() -> list[dict]:
        """Enumerate available WASAPI loopback devices as [{'index', 'name'}, ...]."""
        devices = []
        try:
            with pyaudio.PyAudio() as pa:
                for device in pa.get_loopback_device_info_generator():
                    devices.append({"index": device["index"], "name": device["name"]})
        except Exception as e:
            print(f"[audio] could not enumerate loopback devices: {e}")
        return devices

    def _run(self):
        with pyaudio.PyAudio() as pa:
            device = self._find_loopback_device(pa)
            device_rate = int(device["defaultSampleRate"])
            channels = int(device["maxInputChannels"]) or 2

            frame_ms = 30
            frames_per_buffer = int(device_rate * frame_ms / 1000)

            print(f"[audio] loopback device: {device['name']!r} "
                  f"rate={device_rate} channels={channels}")

            # PyAudioWPatch's blocking stream.read() is unreliable for WASAPI
            # loopback streams - the documented-working pattern is a callback
            # that just hands raw frames off to a queue for processing here.
            raw_queue: "queue.Queue[bytes]" = queue.Queue()

            def _callback(in_data, frame_count, time_info, status):
                raw_queue.put(in_data)
                with self._lock:
                    self._last_read_ts = time.monotonic()
                return (None, pyaudio.paContinue)

            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=device_rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=frames_per_buffer,
                stream_callback=_callback,
            )
            stream.start_stream()

            buffer: list[bytes] = []
            utterance_start = None
            silence_ms = 0
            last_heartbeat = time.monotonic()

            try:
                while not self._stop.is_set():
                    try:
                        raw = raw_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if not self._enabled.is_set():
                        buffer = []
                        utterance_start = None
                        silence_ms = 0
                        continue

                    # downmix to mono int16 for RMS / storage simplicity
                    samples = np.frombuffer(raw, dtype=np.int16)
                    if channels > 1:
                        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
                    mono_bytes = samples.tobytes()

                    level = _rms(mono_bytes)
                    now = time.monotonic()

                    if now - last_heartbeat >= 2.0:
                        print(f"[audio] level={level:.0f} (threshold={config.SILENCE_RMS_THRESHOLD})")
                        last_heartbeat = now

                    if level >= config.SILENCE_RMS_THRESHOLD:
                        if utterance_start is None:
                            utterance_start = now
                            buffer = []
                        buffer.append(mono_bytes)
                        silence_ms = 0
                    else:
                        if utterance_start is not None:
                            buffer.append(mono_bytes)
                            silence_ms += frame_ms

                    duration = (now - utterance_start) if utterance_start else 0
                    should_close = utterance_start is not None and (
                        silence_ms >= config.SILENCE_HANG_SECONDS * 1000
                        or duration >= config.MAX_UTTERANCE_SECONDS
                    )

                    if should_close:
                        if duration >= config.MIN_UTTERANCE_SECONDS:
                            pcm = b"".join(buffer)
                            resampled = _resample(pcm, device_rate, config.SAMPLE_RATE)
                            self.out_queue.put(resampled)
                        buffer = []
                        utterance_start = None
                        silence_ms = 0
            finally:
                stream.stop_stream()
                stream.close()


class MicRecorder:
    """Push-to-talk recording from the real microphone (default WASAPI input
    device) - unrelated to the loopback AudioCapture above. start() begins
    buffering frames; stop() ends the stream and returns the recorded audio
    as mono 16-bit PCM resampled to config.SAMPLE_RATE, ready to transcribe."""

    def __init__(self):
        self._pa = None
        self._stream = None
        self._frames: list[bytes] = []
        self._device_rate = None
        self._channels = None

    def start(self):
        self._frames = []
        self._pa = pyaudio.PyAudio()
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        device = self._pa.get_device_info_by_index(wasapi_info["defaultInputDevice"])
        self._device_rate = int(device["defaultSampleRate"])
        self._channels = int(device["maxInputChannels"]) or 1

        def _callback(in_data, frame_count, time_info, status):
            self._frames.append(in_data)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self._channels,
            rate=self._device_rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=1024,
            stream_callback=_callback,
        )
        self._stream.start_stream()
        print(f"[mic] recording started: {device['name']!r} "
              f"rate={self._device_rate} channels={self._channels}")

    def stop(self) -> bytes:
        """Ends the stream and returns the recorded PCM. Safe to call even if
        start() was never called or stop() was already called."""
        if self._stream is None:
            return b""
        self._stream.stop_stream()
        self._stream.close()
        self._pa.terminate()
        self._stream = None
        self._pa = None

        raw = b"".join(self._frames)
        self._frames = []
        samples = np.frombuffer(raw, dtype=np.int16)
        if self._channels > 1:
            samples = samples.reshape(-1, self._channels).mean(axis=1).astype(np.int16)
        mono_bytes = samples.tobytes()
        resampled = _resample(mono_bytes, self._device_rate, config.SAMPLE_RATE)
        print(f"[mic] recording stopped, {len(resampled)} bytes")
        return resampled


def _resample(pcm_bytes: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    duration = samples.size / src_rate
    dst_len = int(duration * dst_rate)
    if dst_len <= 0:
        return b""
    src_idx = np.linspace(0, samples.size - 1, num=dst_len)
    resampled = np.interp(src_idx, np.arange(samples.size), samples).astype(np.int16)
    return resampled.tobytes()


def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = config.SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM in a minimal WAV header."""
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm_bytes)

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm_bytes
