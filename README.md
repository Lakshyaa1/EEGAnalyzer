# EEGAnalyzer

A complete EEG data acquisition and analysis pipeline for ESP32-based systems, featuring two distinct recording architectures (SPIFFS + STA mode, and self-hosted AP mode) with real-time visualization and offline analysis capabilities.

**Target hardware:** Upside Down Labs BioAmp EXG Pill amplifier, dual-channel configuration on Seeed XIAO nRF52840 Sense or ESP32 dev boards.

---

## Table of Contents

1. [Overview & Architecture](#overview--architecture)
2. [Electrode Placement](#electrode-placement)
3. [Firmware Versions](#firmware-versions)
   - [v1 — SPIFFS + Router (WiFi STA mode)](#v1--spiffs--router-wifi-sta-mode)
   - [v2 — Self-Hosted AP Dashboard](#v2--self-hosted-ap-dashboard)
4. [Installation & Setup](#installation--setup)
5. [Usage](#usage)
   - [Recording (v1)](#recording-v1)
   - [Recording (v2)](#recording-v2)
   - [Live Monitoring](#live-monitoring)
   - [Offline Analysis](#offline-analysis)
6. [Data Format](#data-format)
7. [Troubleshooting](#troubleshooting)
8. [Contributing](#contributing)

---

## Overview & Architecture

This project provides two complementary recording modes:

| Feature | **v1 (SPIFFS + STA)** | **v2 (AP Dashboard)** |
|---------|---|---|
| **Setup Complexity** | Medium (firmware + HTML upload to SPIFFS) | Low (single `.ino` upload) |
| **Network Dependency** | Requires shared WiFi router | Self-contained (ESP32 is the network) |
| **Protocol** | Binary uint16 packets (fixed-size frames) | Text WebSocket (CSV batches) |
| **CSV Recording** | Server-side; lossy `/stream` handler | Browser-side; reliable (buffered in JS) |
| **Real-time View** | HTML dashboard (in-browser) | HTML dashboard (in-browser) |
| **Offline Analysis** | Post-hoc via `analyze_eeg_dataset.py` | Post-hoc via `analyze_eeg_dataset.py` |
| **Python Live FFT** | No | Yes (`eeg_recorder_live.py`) |

### Key Differences at a Glance

**v1 (SPIFFS):**
- Firmware includes HTML index page uploaded separately to the ESP32's SPIFFS filesystem.
- ESP32 joins an existing WiFi network (STA mode), opens an HTTP `/stream` endpoint for CSV logging.
- **Known issue:** WebSocket socket hangs if the browser navigates away. This affected early clinical trials.
- Good for production setups where infrastructure is already available.

**v2 (AP Dashboard):**
- Firmware is entirely self-contained: HTML is compiled into the `.ino` file as a raw string.
- ESP32 creates its own WiFi access point (`EEG-Sensor`, no authentication required).
- Communicates via a single persistent WebSocket with a text-based frame protocol (D/L/S prefixes).
- **Structural fix:** No navigation-sensitive socket, no blocking HTTP handlers; one non-blocking `loop()`.
- Ideal for fieldwork, testing, or when network infrastructure is unavailable.
- Also supports a lightweight Python CLI tool with real-time moving-FFT waterfall visualization.

Both versions produce **identical CSV output** (`timestamp_ms, channel, gpio, adc, packet`), so offline analysis is completely interchangeable.

---

## Electrode Placement

### Subject Setup
- **Reference:** Fpz (midline, forehead)
- **Channel 0 (O1):** Left occipital (visual cortex, back of the head)
  - GPIO: 32 (v2) / configurable (v1)
  - Anatomical region: Primary visual processing
- **Channel 1 (FP1):** Left prefrontal (forehead, above left eyebrow)
  - GPIO: 33 (v2) / configurable (v1)
  - Anatomical region: Executive function, attention, emotion regulation

### Amplification
- **Amplifier:** Upside Down Labs BioAmp EXG Pill (×2.4 MVpp, ×1 MV gain)
- **Bandpass (firmware):** 0.5–45 Hz (hardware + software filtering)
- **Notch (50 Hz AC mains rejection)**
- **ADC:** 12-bit (0–3.3 V, 11 dB attenuation on GPIO32/33)

---

## Firmware Versions

### v1 — SPIFFS + Router (WiFi STA mode)

**File:** `firmware/BioSignal-Recorder-v1/BioSignal-Recorder.ino`

**Upload procedure (Arduino IDE 1.8.x):**
1. Install the ESP32 board package (Boards Manager).
2. Select Board: `ESP32 Dev Module` (or your specific board variant).
3. Install **LittleFS** library (Sketch → Include Library → Manage Libraries → search "LittleFS" by Lorol).
4. Upload filesystem first:
   - **Tools → ESP32 Sketch Data Upload**
   - This uploads `data/index.html` to the ESP32's SPIFFS partition.
5. Then upload the sketch (Sketch → Upload).
6. Open Serial Monitor (9600 baud); note the ESP32's IP address assigned by your router.

**Usage:**
1. Edit the sketch to set your WiFi credentials (SSID/password).
2. Open a web browser and navigate to `http://<esp32-ip>/`.
3. Configure recording duration, channel GPIOs, and electrode labels on the dashboard.
4. Click **Start Logging** to begin.
5. CSV is accumulated server-side and auto-downloaded when the session finishes.
6. Metadata and logs are saved locally.

**Limitations:**
- Requires a stable router + WiFi coverage.
- The `/stream` HTTP handler blocks the main `loop()` while logging, which can cause the WebSocket to hang if the browser navigates during a session.
- GPIO assignment is flexible in the firmware but must match the `eegrecorder-V1.py` protocol mapping.

---

### v2 — Self-Hosted AP Dashboard

**File:** `firmware/Biosignal-Recorder-v2/Biosignal-Recorder-v2.ino`

**Upload procedure (Arduino IDE 1.8.x or IDE 2.x):**
1. Install ESP32 board package.
2. Select Board: `ESP32 Dev Module`.
3. Install **WebSocket** library (Boards Manager → `WebSocketsServer` by Markus Sattler).
4. Open the sketch and upload directly (Sketch → Upload).
   - **No SPIFFS upload needed.** HTML is embedded in the firmware.

**Setup & usage:**
1. After flashing, the ESP32 automatically creates a WiFi AP named `EEG-Sensor` (no password).
2. Connect your laptop or phone to this network.
3. Open a web browser to `http://192.168.4.1/` (the AP's default IP).
4. Configure recording parameters on the dashboard (duration, electrode labels, notes).
5. Click **Start Logging** to begin streaming and recording.
6. CSV data is buffered in the browser and auto-downloaded at session end.
7. Metadata and logs are saved to your local `EEG_Dataset/` folder structure.

**Hardware changes from v1:**
- **Channel 0:** GPIO32 (fixed)
- **Channel 1:** GPIO33 (fixed)
- These are the "recommended" pins for EXG amplifier input on most ESP32 breakouts.

**Advantages:**
- Single upload step (no SPIFFS management).
- No external router dependency; works anywhere.
- Structurally robust: non-blocking event loop, persistent WebSocket, no socket-killing navigation.
- Drop-in replacement for v1 if you're willing to re-wire GPIO32/33.

---

## Installation & Setup

### Prerequisites

**Arduino IDE (1.8.x or 2.x):**
- [Download](https://www.arduino.cc/en/software)
- Install ESP32 board support (Board Manager → `esp32` by Espressif Systems).

**Python 3.8+** (for recording tools):
```bash
pip install websocket-client numpy scipy pandas matplotlib
```

### Directory Structure

```
~/EEGAnalyzer
├── firmware
│   ├── BioSignal-Recorder-v1
│   │   ├── BioSignal-Recorder.ino
│   │   └── data
│   │       └── index.html          # uploaded to SPIFFS
│   └── Biosignal-Recorder-v2
│       └── Biosignal-Recorder-v2.ino  # no SPIFFS needed
├── recorder
│   ├── analyze_eeg_dataset.py       # offline FFT analysis
│   ├── eeg_recorder_live.py         # v2 + live FFT waterfall
│   └── eegrecorder-V1.py            # v1 WebSocket client
└── README.md
```

---

## Usage

### Recording (v1)

1. **Firmware:** Follow v1 upload procedure above.
2. **Python Client:** Run `recorder/eegrecorder-V1.py`
   ```bash
   python eegrecorder-V1.py
   ```
   - Prompted for ESP32 IP (assigned by your router).
   - Prompted for subject name, task, duration, channel assignments, and electrode labels.
   - Opens a WebSocket connection and begins streaming/logging CSV.
   - Closes after the duration expires or on Ctrl+C.

### Recording (v2)

**Option A: Web browser dashboard (simplest)**
1. Firmware flashed and running.
2. Connect to `EEG-Sensor` WiFi network.
3. Open browser to `http://192.168.4.1/`.
4. Configure and click **Start Logging**.

**Option B: Python CLI (with metadata & session management)**
```bash
python recorder/eegrecorder_ap.py
```
   - Same prompts as v1, but connects to `192.168.4.1:81` by default.
   - Saves CSV, metadata, and log to timestamped `EEG_Dataset/` folder.
   - Closes when firmware reports `LOG_COMPLETE` or on Ctrl+C.

### Live Monitoring

**Real-time moving-FFT waterfall (v2 only):**
```bash
python recorder/eeg_recorder_live.py
```
- Combines recording + live visualization in a single tool.
- Displays two 2×2 subplots (one per channel):
  - **Left:** Filtered waveform (last 2 seconds).
  - **Right:** Scrolling waterfall spectrogram (last 10 seconds of history).
- FFT window: 2 seconds (0.5 Hz resolution at 250 Hz sampling).
- Updates every 250 ms (75% overlap for smooth waterfall).
- Reuses exact same filtering (`notch + bandpass`) as offline analyzer.
- Closes when session ends or window is closed.
- Writes identical CSV/metadata/log as `eegrecorder_ap.py`.

---

### Offline Analysis

After recording, analyze the full session with:

```bash
python recorder/analyze_eeg_dataset.py --state <Task> --subject <Name> --mode both
```

**Examples:**
```bash
# Full-session FFT + moving FFT
python analyze_eeg_dataset.py --state Eyes_Closed --subject Lakshya --mode both

# Just moving FFT (spectrogram)
python analyze_eeg_dataset.py --state Eyes_Closed --subject Lakshya --mode stft

# Single channel only
python analyze_eeg_dataset.py --state Eyes_Closed --subject Lakshya --channel 0 --mode fft

# Help
python analyze_eeg_dataset.py --help
```

**Outputs:**
- **PNG plots** saved to `EEG_Dataset/<Subject>/<Date>/<Task>/analysis/`
- **Console summary:** Peak frequency, amplitude, artifact rejection stats, per-channel sampling rate estimation.
- **Filtering applied:** Notch @ 50 Hz (Q=30), bandpass 0.5–45 Hz (4th-order Butterworth), `filtfilt` for zero-phase distortion.
- **Artifact detection:** Median-absolute-deviation (MAD) based gate, rejecting ≥6σ amplitude windows (2 s).

---

## Data Format

### CSV Schema

All recording modes produce identical output:

```csv
timestamp_ms,channel,gpio,adc,packet
0,0,32,2048,0
1,1,33,2050,0
3,0,32,2055,1
4,1,33,2052,1
```

| Column | Type | Notes |
|--------|------|-------|
| `timestamp_ms` | int | Milliseconds since ESP32 boot (or firmware stream start). |
| `channel` | int | 0 (O1, visual cortex) or 1 (FP1, prefrontal). |
| `gpio` | int | GPIO pin number (32 or 33 in v2; configurable in v1). |
| `adc` | int | Raw ADC count (0–4095 for 12-bit). |
| `packet` | int | Per-channel running sample index (used for gap detection). |

### Metadata JSON

Recorded at the start of each session:

```json
{
  "board": "ESP32",
  "firmware": "esp32-eeg-ap-dashboard-2ch",
  "sampling_rate": 250,
  "subject": "Lakshya",
  "task": "Eyes Closed",
  "duration_seconds": 120,
  "channel0_gpio": 32,
  "channel0_electrode": "O1",
  "channel1_gpio": 33,
  "channel1_electrode": "FP1",
  "reference": "Fpz",
  "date": "2026-07-15",
  "start_time": "14:30:45",
  "session_notes": "Alert, no movement"
}
```

### Session Log

Text summary of recording stats:

```
Recording Ended
14:31:45

Recording Duration
120.0 seconds

Samples CH0
30000

Samples CH1
30001

Dropped CH0 (est.)
0

Dropped CH1 (est.)
0

Packet Loss %
0.00%

Effective Sampling Rate
250.02 Hz per channel
```

---

## Troubleshooting

### General

**Q: "ModuleNotFoundError: No module named 'websocket'"**
```bash
pip install websocket-client
```

**Q: My CSV is empty or timestamps are all zeros.**
- Check Serial Monitor output for ADC self-test results (should print min/max/avg values at startup).
- Verify EXG Pill amplifier is powered and electrodes are in contact (not floating).
- Ensure GPIO pins match firmware configuration.

### v1 (SPIFFS)

**Q: "Could not upload filesystem. Flash memory is too small."**
- Your board may not have SPIFFS support. Check board variant in Arduino IDE.
- Reduce `index.html` size (minify/compress) or switch to v2.

**Q: The CSV stops recording mid-session (browser shows ⏸).**
- This is the known socket-hang bug. Workaround: do not navigate in the browser during logging.
- **Fix:** Use v2 instead, which avoids this entirely.

**Q: ESP32 is not connecting to WiFi.**
- Check SSID/password in the sketch (look for `const char* ssid` and `const char* password`).
- Verify your WiFi is on 2.4 GHz (many ESP32s do not support 5 GHz).
- Restart the board and router.

### v2 (AP Dashboard)

**Q: "Failed to connect to 192.168.4.1:81"**
- Verify you are connected to the `EEG-Sensor` WiFi network (not your router).
- Restart the ESP32 (press EN button or power cycle).
- Check that WebSocket server is actually running (Serial Monitor should show "AP ready" and an IP on startup).

**Q: "Connection reset by peer" after a few seconds.**
- The ESP32's default softAP client limit is often 4 stations. Disconnect other devices.
- If using older WebSocket library, update it via Board Manager.

**Q: "ImportError: No module named 'analyze_eeg_dataset'"**
- `eeg_recorder_live.py` imports `filter_signal` from the analyzer. Run both scripts from the same directory:
  ```bash
  cd ~/EEGAnalyzer/recorder
  python eeg_recorder_live.py
  ```

### Live FFT Waterfall

**Q: The spectrogram colors look washed out / oversaturated.**
- The color scale (`vmin=0, vmax=0.05` in `eeg_recorder_live.py`) is a guess at typical voltage magnitudes.
- Inspect your recorded CSV and calculate the actual filtered max/min, then adjust the scale in the source code.

**Q: Real-time FFT is jumpy or updates are slow.**
- Matplotlib's animation on some systems can be CPU-bound. Try reducing `FFT_UPDATE_MS` (currently 250 ms) or skipping every N updates.
- Reduce `WATERFALL_HISTORY_SEC` (currently 10 s) to plot less history.

---

## Sampling Rate & Filtering

Both firmware versions are calibrated to:

- **Nominal sampling rate:** 250 Hz per channel (hard real-time via `vTaskDelayUntil()` on core 0).
- **Actual rate estimation:** Computed from CSV `timestamp_ms` deltas at analysis time (may differ slightly due to clock drift).
- **Notch filter:** 50 Hz (Q=30) to remove AC mains interference.
- **Bandpass filter:** 0.5–45 Hz (4th-order Butterworth, applied with zero-phase `filtfilt`).
- **ADC gain:** 12-bit (0–4095 counts = 0–3.3 V).

All filtering is deterministic and identical between live (v2) and offline analysis. If you modify filter parameters in `analyze_eeg_dataset.py`, the offline analyzer will use them; `eeg_recorder_live.py` imports and reuses them automatically.

---

## Project History

### v1 (SPIFFS + STA mode)
- Initial implementation targeting Arduino IDE 1.8.x.
- HTML dashboard served from embedded SPIFFS filesystem.
- Works well for lab setups with stable WiFi infrastructure.
- Known socket-hang bug during browser navigation (low risk in practice for unattended recording, but structural issue).

### v2 (AP-Dashboard)
- Refactored to eliminate external network dependency.
- Self-contained firmware (HTML baked in as raw C++ string).
- Structural socket-hang bug is gone (no blocking HTTP handlers, persistent WebSocket).
- Added Python CLI tools (`eegrecorder_ap.py`, `eeg_recorder_live.py`) for headless + live-view workflows.
- Tested with real clinical EEG data (ParkinSense wearable, tremor detection pipeline).

---

## Contributing

Found a bug or have a feature request? Open an issue or PR describing:
1. Which firmware version + recorder script you used.
2. Reproduction steps and error messages (include Serial Monitor output if relevant).
3. Expected vs. actual behavior.

Code improvements (especially around filtering robustness, edge-case handling, or new visualization modes) are welcome.

---

## License

[Specify your license here, e.g., MIT, GPL-3.0, etc.]

---

## Acknowledgments

- **Upside Down Labs** for the BioAmp EXG Pill amplifier design and documentation.
- **Espressif** for the ESP32 toolchain and FreeRTOS integration.
- **SciPy / NumPy / Matplotlib** communities for excellent signal processing libraries.

---

## Contact & Support

For questions, suggestions, or collaboration opportunities, please reach out or open an issue on this repository.

**Recommended starting point:**
- New user? → Try v2 with `eeg_recorder_live.py` for instant visual feedback.
- Lab deployment? → v1 if infrastructure exists; v2 if self-contained is preferred.
- Clinical/research? → Record with any tool, then run `analyze_eeg_dataset.py --mode both` for rigorous offline analysis.