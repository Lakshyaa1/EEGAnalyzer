#!/usr/bin/env python3
"""
EEG dataset analyzer.

This script loads a recorded EEG session from EEG_Dataset/, estimates the real
sampling rate from the CSV timestamps, applies a 50 Hz notch filter and a
0.5-45 Hz bandpass filter, then computes an FFT for the selected state.

Examples:
    python analyze_eeg_dataset.py --state Eyes_Closed --subject Lakshya
    python analyze_eeg_dataset.py --state Blink --subject Unknown --channel 0
    python analyze_eeg_dataset.py --state Meditation --mode stft
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt, iirnotch, spectrogram


DEFAULT_DATASET_ROOT = Path("EEG_Dataset")
NOTCH_FREQ_HZ = 50.0
NOTCH_Q = 30.0
BANDPASS_LOW_HZ = 0.5
BANDPASS_HIGH_HZ = 45.0
ARTIFACT_WINDOW_SEC = 2.0
ARTIFACT_SIGMA_MULTIPLIER = 6.0
ADC_MAX_COUNTS = 4095.0
ADC_REF_VOLTAGE = 3.3
ARTIFACT_MIN_THRESHOLD_VOLTS = 0.24
CHANNEL_LOCATIONS = {
    0: "O1 (back of the brain)",
    1: "Fp1 (forehead)",
}


@dataclass(frozen=True)
class SessionPaths:
    folder: Path
    csv_path: Path
    metadata_path: Path


def normalize_name(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def discover_session(root: Path, subject: Optional[str], state: str, date: Optional[str]) -> SessionPaths:
    state_key = normalize_name(state)
    subject_key = normalize_name(subject) if subject else None

    candidates: List[SessionPaths] = []
    for csv_path in root.rglob("recording.csv"):
        session_folder = csv_path.parent
        metadata_path = session_folder / "metadata.json"
        if not metadata_path.exists():
            continue

        folder_parts = {normalize_name(part) for part in session_folder.parts}
        if normalize_name(session_folder.name) != state_key:
            continue
        if subject_key and subject_key not in folder_parts:
            continue
        if date and date not in folder_parts:
            continue

        candidates.append(SessionPaths(session_folder, csv_path, metadata_path))

    if not candidates:
        raise FileNotFoundError(
            f"Could not find a session for state={state!r}, subject={subject!r}, date={date!r} under {root}"
        )

    if len(candidates) > 1:
        names = "\n".join(f"  - {item.folder}" for item in candidates)
        raise ValueError(
            "Multiple matching sessions found. Please narrow the search with --subject and/or --date:\n"
            f"{names}"
        )

    return candidates[0]


def load_session(paths: SessionPaths) -> Tuple[pd.DataFrame, Dict[str, object]]:
    df = pd.read_csv(paths.csv_path)
    with open(paths.metadata_path, "r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    return df, metadata


def estimate_fs_by_channel(df: pd.DataFrame) -> Dict[int, float]:
    rates: Dict[int, float] = {}
    for channel_value, channel_df in df.groupby("channel"):
        ordered = channel_df.sort_values("timestamp_ms")
        sample_count = len(ordered)
        if sample_count < 2:
            continue
        span_ms = float(ordered["timestamp_ms"].iloc[-1] - ordered["timestamp_ms"].iloc[0])
        if span_ms <= 0:
            continue
        rates[int(channel_value)] = (sample_count - 1) / (span_ms / 1000.0)
    return rates


def estimate_fs_from_rows(df: pd.DataFrame) -> float:
    if df.empty:
        raise ValueError("CSV is empty.")

    by_channel = estimate_fs_by_channel(df)
    if not by_channel:
        span_s = float(df["timestamp_ms"].iloc[-1] - df["timestamp_ms"].iloc[0]) / 1000.0
        if span_s <= 0:
            raise ValueError("Unable to estimate sampling rate from timestamps.")
        return (len(df) - 1) / span_s

    return float(np.median(list(by_channel.values())))


def select_channels(df: pd.DataFrame, channel: str) -> List[int]:
    available = sorted(int(ch) for ch in df["channel"].dropna().unique())
    if channel == "all":
        return available

    try:
        selected = int(channel)
    except ValueError as exc:
        raise ValueError("--channel must be 'all' or an integer channel index") from exc

    if selected not in available:
        raise ValueError(f"Channel {selected} is not present in this session. Available channels: {available}")
    return [selected]


def design_filters(fs: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if fs <= 0:
        raise ValueError("Sampling rate must be positive.")

    b_notch, a_notch = iirnotch(NOTCH_FREQ_HZ, NOTCH_Q, fs)
    nyquist = fs / 2.0
    high = min(BANDPASS_HIGH_HZ, nyquist * 0.95)
    if BANDPASS_LOW_HZ >= high:
        raise ValueError(
            f"Bandpass upper bound {high:.2f} Hz is not above the lower bound {BANDPASS_LOW_HZ:.2f} Hz for fs={fs:.2f}."
        )
    b_band, a_band = butter(4, [BANDPASS_LOW_HZ, high], btype="bandpass", fs=fs)
    return b_notch, a_notch, b_band, a_band


def filter_signal(signal: np.ndarray, fs: float) -> np.ndarray:
    volts = adc_to_volts(signal)
    centered = volts - np.mean(volts)
    b_notch, a_notch, b_band, a_band = design_filters(fs)
    notched = filtfilt(b_notch, a_notch, centered)
    return filtfilt(b_band, a_band, notched)


def adc_to_volts(signal: np.ndarray) -> np.ndarray:
    return signal.astype(np.float64) * (ADC_REF_VOLTAGE / ADC_MAX_COUNTS)


def robust_artifact_threshold(signal: np.ndarray) -> float:
    centered = signal.astype(np.float64) - np.median(signal)
    mad = float(np.median(np.abs(centered)))
    robust_sigma = 1.4826 * mad
    threshold = ARTIFACT_SIGMA_MULTIPLIER * robust_sigma
    return max(ARTIFACT_MIN_THRESHOLD_VOLTS, threshold)


def reject_artifact_windows(signal: np.ndarray, fs: float, window_sec: float = ARTIFACT_WINDOW_SEC) -> Tuple[np.ndarray, Dict[str, float]]:
    if len(signal) == 0:
        return signal, {"threshold": 0.0, "windows_total": 0, "windows_kept": 0, "windows_rejected": 0, "rejected_pct": 0.0}

    threshold = robust_artifact_threshold(signal)
    window_samples = max(8, int(round(window_sec * fs)))
    accepted: List[np.ndarray] = []
    windows_total = 0
    windows_kept = 0

    for start in range(0, len(signal), window_samples):
        chunk = signal[start : start + window_samples]
        if len(chunk) < max(8, window_samples // 2):
            continue
        windows_total += 1
        centered_chunk = chunk.astype(np.float64) - np.median(chunk)
        peak_abs = float(np.max(np.abs(centered_chunk)))
        if peak_abs <= threshold:
            accepted.append(chunk)
            windows_kept += 1

    cleaned = np.concatenate(accepted) if accepted else np.array([], dtype=signal.dtype)
    windows_rejected = windows_total - windows_kept
    rejected_pct = (windows_rejected / windows_total * 100.0) if windows_total > 0 else 0.0
    stats = {
        "threshold": float(threshold),
        "windows_total": float(windows_total),
        "windows_kept": float(windows_kept),
        "windows_rejected": float(windows_rejected),
        "rejected_pct": float(rejected_pct),
    }
    return cleaned, stats


def compute_fft(signal: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray]:
    n = len(signal)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    fft_vals = np.fft.rfft(signal)
    magnitude = np.abs(fft_vals) / n
    return freqs, magnitude


def summarize_signal(signal: np.ndarray, fs: float) -> Tuple[float, float]:
    freqs, magnitude = compute_fft(signal, fs)
    if len(freqs) <= 1:
        return 0.0, 0.0

    positive = freqs > 0
    if not np.any(positive):
        return 0.0, 0.0

    peak_index = int(np.argmax(magnitude[positive]))
    peak_freq = float(freqs[positive][peak_index])
    peak_amp = float(magnitude[positive][peak_index])
    return peak_freq, peak_amp


def plot_fft(ax: plt.Axes, freqs: np.ndarray, magnitude: np.ndarray, channel: int, fs: float) -> None:
    mask = freqs <= 60.0
    ax.plot(freqs[mask], magnitude[mask], color="#1f2937", linewidth=1.2)
    location = CHANNEL_LOCATIONS.get(channel, "unknown location")
    ax.set_title(f"Channel {channel} FFT - {location} (fs={fs:.2f} Hz)")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Magnitude (V)")
    ax.grid(True, alpha=0.25)
    ax.axvspan(0.5, 4.0, color="#ef4444", alpha=0.08)
    ax.axvspan(4.0, 8.0, color="#f97316", alpha=0.08)
    ax.axvspan(8.0, 13.0, color="#22c55e", alpha=0.08)
    ax.axvspan(13.0, 30.0, color="#3b82f6", alpha=0.08)
    ax.axvspan(30.0, 45.0, color="#8b5cf6", alpha=0.08)
    ax.set_xlim(0, 60)
    ax.text(
        0.5,
        -0.25,
        "Delta 0.5-4 Hz | Theta 4-8 Hz | Alpha 8-13 Hz | Beta 13-30 Hz | Gamma 30-45 Hz",
        transform=ax.transAxes,
        ha="center",
        va="top",
        fontsize=9,
        color="#374151",
    )


def plot_stft(ax: plt.Axes, signal: np.ndarray, fs: float, channel: int, window_sec: float) -> None:
    window_samples = max(16, int(round(window_sec * fs)))
    overlap = max(0, window_samples // 2)
    freqs, times, spec = spectrogram(
        signal,
        fs=fs,
        window="hann",
        nperseg=window_samples,
        noverlap=overlap,
        scaling="spectrum",
        mode="magnitude",
    )
    mask = freqs <= 60.0
    mesh = ax.pcolormesh(times, freqs[mask], spec[mask], shading="auto", cmap="magma")
    ax.set_title(f"Channel {channel} moving FFT / spectrogram")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_ylim(0, 60)
    ax.figure.colorbar(mesh, ax=ax, label="Magnitude")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze EEG dataset recordings.")
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT, help="EEG dataset root folder")
    parser.add_argument("--subject", type=str, default=None, help="Subject folder name, for example Lakshya")
    parser.add_argument("--date", type=str, default=None, help="Session date folder, for example 2026-07-13")
    parser.add_argument("--state", type=str, required=True, help="State folder name, for example Eyes_Closed")
    parser.add_argument("--channel", type=str, default="all", help="Channel index or 'all'")
    parser.add_argument("--no-artifact-filter", action="store_true", help="Skip amplitude-based artifact rejection")
    parser.add_argument(
        "--mode",
        choices=("fft", "stft", "both"),
        default="fft",
        help="Plot a single FFT, a moving FFT, or both",
    )
    parser.add_argument("--window-sec", type=float, default=4.0, help="STFT window length in seconds")
    parser.add_argument("--output", type=Path, default=None, help="Optional output image path")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()

    session = discover_session(args.dataset_root, args.subject, args.state, args.date)
    df, metadata = load_session(session)

    if "timestamp_ms" not in df.columns or "channel" not in df.columns or "adc" not in df.columns:
        raise ValueError("CSV must contain timestamp_ms, channel, and adc columns.")

    selected_channels = select_channels(df, args.channel)
    per_channel_fs = estimate_fs_by_channel(df)
    combined_fs = estimate_fs_from_rows(df)

    print(f"Session: {session.folder}")
    print(f"Metadata sampling_rate: {metadata.get('sampling_rate', 'unknown')} Hz")
    print(f"Estimated combined sampling rate: {combined_fs:.2f} Hz")
    for channel, fs_value in sorted(per_channel_fs.items()):
        print(f"Estimated channel {channel} sampling rate: {fs_value:.2f} Hz")
    for channel in selected_channels:
        print(f"Channel {channel}: {CHANNEL_LOCATIONS.get(channel, 'unknown location')}")

    filtered_by_channel: Dict[int, np.ndarray] = {}
    fs_by_channel: Dict[int, float] = {}

    for channel in selected_channels:
        channel_df = df[df["channel"] == channel].sort_values("timestamp_ms")
        if len(channel_df) < 8:
            raise ValueError(f"Channel {channel} does not have enough samples for FFT analysis.")

        fs_value = per_channel_fs.get(channel, combined_fs)
        raw = channel_df["adc"].to_numpy(dtype=np.float64)
        filtered = filter_signal(raw, fs_value)

        if args.no_artifact_filter:
            artifact_stats = {
                "threshold": 0.0,
                "windows_total": 0.0,
                "windows_kept": 0.0,
                "windows_rejected": 0.0,
                "rejected_pct": 0.0,
            }
            cleaned = filtered
        else:
            cleaned, artifact_stats = reject_artifact_windows(filtered, fs_value)
            if len(cleaned) < 8:
                print(f"Channel {channel}: artifact gate removed too much data, using filtered signal without rejection.")
                cleaned = filtered
                artifact_stats["windows_kept"] = artifact_stats["windows_total"]
                artifact_stats["windows_rejected"] = 0.0
                artifact_stats["rejected_pct"] = 0.0

        filtered_by_channel[channel] = cleaned
        fs_by_channel[channel] = fs_value

        peak_freq, peak_amp = summarize_signal(cleaned, fs_value)
        duration_s = (len(raw) - 1) / fs_value if fs_value > 0 else 0.0
        print(
            f"Channel {channel}: samples={len(raw)} duration={duration_s:.2f}s "
            f"peak={peak_freq:.2f} Hz amplitude={peak_amp:.6f} V"
        )
        if not args.no_artifact_filter:
            print(
                f"Channel {channel}: artifact threshold={artifact_stats['threshold']:.3f} V, "
                f"kept={int(artifact_stats['windows_kept'])}/{int(artifact_stats['windows_total'])} windows, "
                f"rejected={artifact_stats['rejected_pct']:.1f}%"
            )

    if args.mode == "fft":
        fig, axes = plt.subplots(len(selected_channels), 1, figsize=(12, 4 * len(selected_channels)), squeeze=False)
        for axis, channel in zip(axes.flat, selected_channels):
            freqs, magnitude = compute_fft(filtered_by_channel[channel], fs_by_channel[channel])
            plot_fft(axis, freqs, magnitude, channel, fs_by_channel[channel])
        fig.suptitle(f"EEG FFT: {session.folder.name}", y=0.995)
        fig.tight_layout()
    elif args.mode == "stft":
        fig, axes = plt.subplots(len(selected_channels), 1, figsize=(12, 4 * len(selected_channels)), squeeze=False)
        for axis, channel in zip(axes.flat, selected_channels):
            plot_stft(axis, filtered_by_channel[channel], fs_by_channel[channel], channel, args.window_sec)
        fig.suptitle(f"EEG Moving FFT: {session.folder.name}", y=0.995)
        fig.tight_layout()
    else:
        fig, axes = plt.subplots(len(selected_channels), 2, figsize=(15, 4 * len(selected_channels)), squeeze=False)
        for row, channel in enumerate(selected_channels):
            freqs, magnitude = compute_fft(filtered_by_channel[channel], fs_by_channel[channel])
            plot_fft(axes[row, 0], freqs, magnitude, channel, fs_by_channel[channel])
            plot_stft(axes[row, 1], filtered_by_channel[channel], fs_by_channel[channel], channel, args.window_sec)
        fig.suptitle(f"EEG FFT Analysis: {session.folder.name}", y=0.995)
        fig.tight_layout()

    output_path = args.output
    if output_path is None:
        output_dir = session.folder / "analysis"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{session.folder.name}_{args.mode}.png"

    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    print(f"Saved plot to: {output_path}")

    if "agg" not in plt.get_backend().lower():
        plt.show()


if __name__ == "__main__":
    main()