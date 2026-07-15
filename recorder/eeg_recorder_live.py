#!/usr/bin/env python3
"""
EEG DATA RECORDER — Live view edition
---------------------------------------
Combines the AP-dashboard WebSocket recorder with a real-time moving-FFT
(waterfall spectrogram) display for both channels while data streams in.

- CSV/metadata/log output format is UNCHANGED from eegrecorder_ap.py:
    timestamp_ms, channel, gpio, adc, packet
  so analyze_eeg_dataset.py continues to work unmodified for the full
  offline session analysis after recording finishes.
- The live view is a lightweight, causal, chunked FFT purely for visual
  feedback during acquisition (electrode-quality checks, artifact
  spotting). It reuses filter_signal()/design_filters() from
  analyze_eeg_dataset.py so both tools always apply identical filtering.
  It is NOT a substitute for the offline analysis run afterward:
  filtfilt is applied per-window here (small edge effects at each window
  boundary), whereas the offline script filters the full continuous
  session at once.

Requires: websocket-client, matplotlib, numpy, scipy, pandas (same deps
as your existing scripts) — run:
    pip install websocket-client matplotlib numpy scipy pandas
"""

from __future__ import annotations

import sys
import time
import csv
import json
import signal
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import websocket

# Reuse the exact same filtering logic as the offline analyzer so live
# and offline views never disagree.
from analyze_eeg_dataset import design_filters, filter_signal, adc_to_volts

# --- Configuration ---
FS = 250.0                      # must match SAMPLE_RATE_HZ in the .ino
FFT_WINDOW_SEC = 2.0            # moving-FFT window length (see rationale above)
FFT_UPDATE_MS = 250             # -> 75% overlap at a 2 s window
WATERFALL_HISTORY_SEC = 10.0    # how much scrolling history to show
FREQ_DISPLAY_MAX_HZ = 60.0

TASKS = {
    "1": "Eyes Open", "2": "Eyes Closed", "3": "Mental Arithmetic",
    "4": "Reading", "5": "Deep Breathing", "6": "Meditation",
    "7": "Blink", "8": "Jaw Clench", "9": "Eye Movement",
    "10": "Speaking", "11": "Custom"
}
CHANNEL_LOCATIONS = {0: "O1 (back of the brain)", 1: "Fp1 (forehead)"}


# --- Global State ---
class State:
    meta: Dict[str, Any] = {}

    is_recording: bool = False
    stop_requested: bool = False
    logging_active: bool = False
    log_complete_evt: threading.Event = threading.Event()

    first_line_time: Optional[float] = None

    samples_ch0: int = 0
    samples_ch1: int = 0
    lines_received: int = 0
    dropped_ch0: int = 0
    dropped_ch1: int = 0
    last_idx_ch0: int = -1
    last_idx_ch1: int = -1

    csv_file: Any = None
    csv_writer: Any = None
    ws_app: Optional[websocket.WebSocketApp] = None

    # Live-view ring buffers: raw ADC counts, most-recent-last
    buf_lock: threading.Lock = threading.Lock()
    ring_ch0: deque = deque(maxlen=int(FS * FFT_WINDOW_SEC))
    ring_ch1: deque = deque(maxlen=int(FS * FFT_WINDOW_SEC))


# --- WebSocket handlers (same protocol as eegrecorder_ap.py) ---
def on_open(ws: websocket.WebSocketApp) -> None:
    try:
        print("\nConnected.")
        cmd = f"C:START:{State.meta['duration_seconds']}"
        print(f"Sending: {cmd}")
        ws.send(cmd)
    except Exception as e:
        print(f"\n[Error] Failed to send start command: {e}")


def _handle_log_line(line: str) -> None:
    parts = line.split(",")
    if len(parts) != 5:
        return
    try:
        timestamp_ms = int(parts[0])
        channel = int(parts[1])
        gpio = int(parts[2])
        adc = int(parts[3])
        packet_idx = int(parts[4])
    except ValueError:
        return
    if channel not in (0, 1):
        return

    if State.first_line_time is None:
        State.first_line_time = time.time()

    if channel == 0:
        if State.last_idx_ch0 != -1 and packet_idx != State.last_idx_ch0 + 1:
            gap = packet_idx - State.last_idx_ch0 - 1
            if gap > 0:
                State.dropped_ch0 += gap
        State.last_idx_ch0 = packet_idx
        State.samples_ch0 += 1
    else:
        if State.last_idx_ch1 != -1 and packet_idx != State.last_idx_ch1 + 1:
            gap = packet_idx - State.last_idx_ch1 - 1
            if gap > 0:
                State.dropped_ch1 += gap
        State.last_idx_ch1 = packet_idx
        State.samples_ch1 += 1

    State.lines_received += 1
    State.csv_writer.writerow([timestamp_ms, channel, gpio, adc, packet_idx])

    with State.buf_lock:
        if channel == 0:
            State.ring_ch0.append(adc)
        else:
            State.ring_ch1.append(adc)


def on_message(ws: websocket.WebSocketApp, message: str) -> None:
    if not message:
        return
    prefix = message[0]

    if prefix == "L":
        if not (State.is_recording and State.logging_active):
            return
        for line in message[1:].splitlines():
            line = line.strip()
            if line and line != "timestamp_ms,channel,gpio,adc,packet":
                _handle_log_line(line)

    elif prefix == "S":
        parts = message[2:].split(":")
        status = parts[0]
        if status == "LOG_STARTED":
            print(f"\n[Firmware] Logging started: {parts[1] if len(parts) > 1 else '?'}")
            State.logging_active = True
        elif status in ("LOG_COMPLETE", "LOG_STOPPED"):
            print(f"\n[Firmware] {status}")
            State.logging_active = False
            State.stop_requested = True
            State.log_complete_evt.set()
        elif status == "LOG_ERROR":
            print(f"\n[Firmware] LOG_ERROR: {':'.join(parts[1:])}")
            State.logging_active = False
            State.stop_requested = True
            State.log_complete_evt.set()


def on_error(ws, error) -> None:
    print(f"\n[WebSocket Error] {error}")
    State.stop_requested = True
    State.log_complete_evt.set()


def on_close(ws, code, msg) -> None:
    State.stop_requested = True
    State.log_complete_evt.set()


# --- CLI setup (same prompts as eegrecorder_ap.py) ---
def get_input(prompt: str, default: str) -> str:
    user_in = input(f"{prompt} [{default}]: ").strip()
    return user_in if user_in else default


def setup_session() -> Dict[str, Any]:
    print("====================================================")
    print("     EEG DATA RECORDER — Live view + logging")
    print("====================================================")

    meta = {
        "board": "ESP32",
        "firmware": "esp32-eeg-ap-dashboard-2ch",
        "sampling_rate": int(FS),
        "ip": get_input("ESP32 AP IP", "192.168.4.1"),
        "subject": get_input("Subject Name", "Unknown"),
    }

    print("\nTasks:")
    for k, v in TASKS.items():
        print(f"  {k}) {v}")
    task_idx = get_input("Task", "1")
    meta["task"] = get_input("Task Name", "Custom Task") if task_idx == "11" else TASKS.get(task_idx, "Unknown Task")

    try:
        meta["duration_seconds"] = int(get_input("Recording Duration (seconds)", "120"))
    except ValueError:
        meta["duration_seconds"] = 120

    meta["channel0_gpio"] = 32
    meta["channel1_gpio"] = 33
    print("\nElectrodes:")
    meta["channel0_electrode"] = get_input("Channel 0 (GPIO32) Electrode", "O1")
    meta["channel1_electrode"] = get_input("Channel 1 (GPIO33) Electrode", "O2")
    meta["reference"] = get_input("Reference", "Fpz")
    meta["session_notes"] = input("\nSession Notes (optional): ").strip()

    print("\n===================================")
    print(f"Subject   {meta['subject']}   Task {meta['task']}   Duration {meta['duration_seconds']}s")
    print("===================================")
    input("\nConnect to the 'EEG-Sensor' WiFi AP, then press ENTER to begin...")
    return meta


def create_directory(meta: Dict[str, Any]) -> Path:
    date_str = datetime.now().strftime("%Y-%m-%d")
    base_path = Path("EEG_Dataset") / meta["subject"].replace(" ", "_") / date_str / meta["task"].replace(" ", "_")
    folder = base_path
    counter = 1
    while folder.exists():
        folder = base_path.with_name(f"{base_path.name}_{counter:02d}")
        counter += 1
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def signal_handler(sig, frame):
    print("\n[INFO] CTRL+C — sending C:STOP...")
    try:
        if State.ws_app and State.ws_app.sock and State.ws_app.sock.connected:
            State.ws_app.send("C:STOP")
    except Exception:
        pass
    State.stop_requested = True
    State.log_complete_evt.set()


# --- Live view (matplotlib animation) ---
class LiveView:
    """Scrolling waterfall spectrogram for both channels, updated from ring buffers."""

    def __init__(self):
        self.window_samples = int(FS * FFT_WINDOW_SEC)
        self.history_cols = int(WATERFALL_HISTORY_SEC * 1000 / FFT_UPDATE_MS)

        # Frequency bins for this window length
        self.freqs = np.fft.rfftfreq(self.window_samples, d=1.0 / FS)
        self.freq_mask = self.freqs <= FREQ_DISPLAY_MAX_HZ
        self.n_freq_bins = int(np.sum(self.freq_mask))

        self.history_ch0 = np.zeros((self.n_freq_bins, self.history_cols))
        self.history_ch1 = np.zeros((self.n_freq_bins, self.history_cols))

        self.fig, self.axes = plt.subplots(2, 2, figsize=(13, 7))
        self.fig.suptitle(
            f"Live EEG — {FFT_WINDOW_SEC:.0f}s FFT window, {FFT_UPDATE_MS}ms update "
            f"(freq resolution {FS / self.window_samples:.2f} Hz)"
        )

        self.wave_lines = {}
        self.wf_images = {}
        for row, ch in enumerate((0, 1)):
            wave_ax = self.axes[row, 0]
            (line,) = wave_ax.plot([], [], color="#1f2937", linewidth=0.8)
            wave_ax.set_xlim(0, FFT_WINDOW_SEC)
            wave_ax.set_ylim(-1.7, 1.7)
            wave_ax.set_title(f"CH{ch} filtered waveform ({CHANNEL_LOCATIONS.get(ch, '')})")
            wave_ax.set_xlabel("Time (s, last window)")
            wave_ax.set_ylabel("Volts")
            self.wave_lines[ch] = line

            wf_ax = self.axes[row, 1]
            img = wf_ax.imshow(
                np.zeros((self.n_freq_bins, self.history_cols)),
                aspect="auto", origin="lower", cmap="magma",
                extent=[-WATERFALL_HISTORY_SEC, 0, 0, FREQ_DISPLAY_MAX_HZ],
                vmin=0, vmax=0.05,
            )
            wf_ax.set_title(f"CH{ch} moving FFT (waterfall)")
            wf_ax.set_xlabel("Time (s ago)")
            wf_ax.set_ylabel("Frequency (Hz)")
            for lo, hi, color in [(0.5, 4, "w"), (4, 8, "w"), (8, 13, "w"), (13, 30, "w"), (30, 45, "w")]:
                wf_ax.axhline(hi, color=color, alpha=0.15, linewidth=0.6)
            self.wf_images[ch] = img

        self.fig.tight_layout(rect=[0, 0, 1, 0.95])
        self.status_text = self.fig.text(0.01, 0.01, "", fontsize=9, color="#374151")

    def _window_or_none(self, ring: deque):
        with State.buf_lock:
            if len(ring) < self.window_samples:
                return None
            return np.array(ring, dtype=np.float64)

    def _update_channel(self, ch: int, raw_window: Optional[np.ndarray]):
        if raw_window is None:
            return
        try:
            filtered = filter_signal(raw_window, FS)  # notch + bandpass, in volts
        except Exception:
            return

        t = np.arange(len(filtered)) / FS
        self.wave_lines[ch].set_data(t, filtered)

        mag = np.abs(np.fft.rfft(filtered)) / len(filtered)
        mag = mag[self.freq_mask]

        hist = self.history_ch0 if ch == 0 else self.history_ch1
        hist[:, :-1] = hist[:, 1:]
        hist[:, -1] = mag
        self.wf_images[ch].set_data(hist)

    def update(self, _frame):
        raw0 = self._window_or_none(State.ring_ch0)
        raw1 = self._window_or_none(State.ring_ch1)
        self._update_channel(0, raw0)
        self._update_channel(1, raw1)

        elapsed = time.time() - State.first_line_time if State.first_line_time else 0.0
        remaining = max(0.0, State.meta.get("duration_seconds", 0) - elapsed)
        self.status_text.set_text(
            f"CH0: {State.samples_ch0} samples (drop~{State.dropped_ch0})  |  "
            f"CH1: {State.samples_ch1} samples (drop~{State.dropped_ch1})  |  "
            f"Elapsed {elapsed:.0f}s  Remaining {remaining:.0f}s"
        )

        artists = list(self.wave_lines.values()) + list(self.wf_images.values()) + [self.status_text]
        return artists

    def run(self):
        # interval in ms; blit=False because we're resizing axes limits/text each frame
        self.anim = animation.FuncAnimation(
            self.fig, self.update, interval=FFT_UPDATE_MS, blit=False, cache_frame_data=False
        )
        plt.show()  # blocks until the window is closed


def finalize_recording(folder: Path):
    State.stop_requested = True
    State.is_recording = False
    if State.ws_app:
        State.ws_app.close()
    if State.csv_file:
        State.csv_file.close()

    end_dt = datetime.now()
    actual_duration = (time.time() - State.first_line_time) if State.first_line_time else 0.0
    total_s = State.samples_ch0 + State.samples_ch1
    avg_rate = (total_s / 2.0) / actual_duration if actual_duration > 0 else 0.0
    total_expected = (State.samples_ch0 + State.dropped_ch0) + (State.samples_ch1 + State.dropped_ch1)
    final_loss_pct = ((State.dropped_ch0 + State.dropped_ch1) / total_expected * 100) if total_expected > 0 else 0.0

    meta_to_save = State.meta.copy()
    meta_to_save.pop("ip", None)
    with open(folder / "metadata.json", "w") as f:
        json.dump(meta_to_save, f, indent=4)

    log_content = (
        f"Recording Ended\n{end_dt.strftime('%H:%M:%S')}\n\n"
        f"Recording Duration\n{actual_duration:.1f} seconds\n\n"
        f"Samples CH0\n{State.samples_ch0}\n\n"
        f"Samples CH1\n{State.samples_ch1}\n\n"
        f"Dropped CH0 (est.)\n{State.dropped_ch0}\n\n"
        f"Dropped CH1 (est.)\n{State.dropped_ch1}\n\n"
        f"Packet Loss %\n{final_loss_pct:.2f}%\n\n"
        f"Effective Sampling Rate\n{avg_rate:.2f} Hz per channel\n"
    )
    with open(folder / "recording_log.txt", "w") as f:
        f.write(log_content)

    print(f"\nRecording saved to:\n{folder.resolve()}")
    print(f"Run the full offline analysis with:\n"
          f"  python analyze_eeg_dataset.py --state {State.meta['task'].replace(' ', '_')} "
          f"--subject {State.meta['subject'].replace(' ', '_')} --mode both")


def main():
    signal.signal(signal.SIGINT, signal_handler)

    State.meta = setup_session()
    folder = create_directory(State.meta)

    start_dt = datetime.now()
    State.meta["date"] = start_dt.strftime("%Y-%m-%d")
    State.meta["start_time"] = start_dt.strftime("%H:%M:%S")

    State.csv_file = open(folder / "recording.csv", mode="w", newline="")
    State.csv_writer = csv.writer(State.csv_file)
    State.csv_writer.writerow(["timestamp_ms", "channel", "gpio", "adc", "packet"])

    ws_url = f"ws://{State.meta['ip']}:81/"
    print(f"Connecting to {ws_url}")
    State.ws_app = websocket.WebSocketApp(
        ws_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close
    )
    ws_thread = threading.Thread(target=State.ws_app.run_forever, daemon=True)
    ws_thread.start()
    State.is_recording = True

    # Live view runs on the MAIN thread (matplotlib requirement) and blocks
    # until either the window is closed or, in the background, a watcher
    # thread closes it once the firmware reports completion.
    view = LiveView()

    def watch_completion():
        State.log_complete_evt.wait()
        time.sleep(0.5)  # let the last FFT frame render
        plt.close(view.fig)

    watcher = threading.Thread(target=watch_completion, daemon=True)
    watcher.start()

    try:
        view.run()
    finally:
        finalize_recording(folder)


if __name__ == "__main__":
    main()
