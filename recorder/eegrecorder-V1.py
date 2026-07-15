#!/usr/bin/env python3
"""
EEG DATA RECORDER v1.0
----------------------
CLI application to acquire, validate, and save EEG data from an ESP32 via WebSocket.
Strictly for data acquisition. No DSP, no GUI, no analysis.
"""

import sys
import time
import json
import csv
import threading
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
import websocket

# --- Configuration & Constants ---
DEBUG_PACKET_INFO = False  # Set to True to print packet-level details during stream

FS = 256.0
SAMPLE_INTERVAL_MS = 1000.0 / FS

# The exact protocol mapping derived from the HTML reference
GPIO_PROTOCOL_MAP = {
    32: "4",
    33: "5",
    34: "6",
    35: "7",
    36: "0",
    37: "1",
    38: "2",
    39: "3"
}

TASKS = {
    "1": "Eyes Open", "2": "Eyes Closed", "3": "Mental Arithmetic",
    "4": "Reading", "5": "Deep Breathing", "6": "Meditation",
    "7": "Blink", "8": "Jaw Clench", "9": "Eye Movement",
    "10": "Speaking", "11": "Custom"
}

# --- Global State ---
class State:
    meta: Dict[str, Any] = {}
    
    is_recording: bool = False
    stop_requested: bool = False
    
    first_packet_time: Optional[float] = None
    
    samples_ch0: int = 0
    samples_ch1: int = 0
    packets_received: int = 0
    dropped_packets: int = 0
    samples_per_packet: int = 0
    
    last_packet_global: int = -1

    csv_file: Any = None
    csv_writer: Any = None
    ws_app: Optional[websocket.WebSocketApp] = None

# --- WebSocket Handlers ---
def on_open(ws: websocket.WebSocketApp) -> None:
    """Initialize ESP32 stream exactly like the HTML interface."""
    try:
        print("\nConnected.")
        
        # 2 channels
        msg_init = "29"
        print(f"Sending: {msg_init}")
        ws.send(msg_init)
        time.sleep(0.1)
        
        # Channel 0 config
        msg_ch0 = f"{State.meta['sampling_rate']}{GPIO_PROTOCOL_MAP[State.meta['channel0_gpio']]}"
        print(f"Sending: {msg_ch0}")
        ws.send(msg_ch0)
        time.sleep(0.1)
        
        # Channel 1 config
        msg_ch1 = f"{State.meta['sampling_rate']}{GPIO_PROTOCOL_MAP[State.meta['channel1_gpio']]}"
        print(f"Sending: {msg_ch1}")
        ws.send(msg_ch1)
        
        print("Receiving data...\n")
        
    except Exception as e:
        print(f"\n[Error] Failed to send initialization commands: {e}")

def on_message(ws: websocket.WebSocketApp, message: bytes) -> None:
    """Decode binary payload and write to CSV."""
    if not State.is_recording or State.stop_requested:
        return

    try:
        if State.first_packet_time is None:
            State.first_packet_time = time.time()

        data = np.frombuffer(message, dtype=np.uint16)
        
        # Validate Packet Structure
        if len(data) < 3:
            print(f"\n[Warning] Invalid packet length: {len(data)}")
            return

        channel_idx = int(data[-1])
        packet_num = int(data[-2])
        adc_samples = data[:-2]
        current_samples_per_packet = len(adc_samples)

        if channel_idx not in [0, 1]:
            print(f"\n[Warning] Unexpected channel index: {channel_idx}")
            return

        # Track and validate samples per packet
        if State.samples_per_packet == 0:
            State.samples_per_packet = current_samples_per_packet
        elif State.samples_per_packet != current_samples_per_packet:
            print(f"\n[Warning] Samples per packet changed from {State.samples_per_packet} to {current_samples_per_packet}")
            State.samples_per_packet = current_samples_per_packet

        if DEBUG_PACKET_INFO:
            print(f"\nCH{channel_idx} Packet {packet_num} Samples {current_samples_per_packet} Bytes {len(message)}")

        State.packets_received += 1

        # Global Packet Loss Detection (Firmware wraps 0-100, modulo 101)
        if State.last_packet_global != -1:
            expected = (State.last_packet_global + 1) % 101
            if packet_num != expected:
                diff = (packet_num - expected) % 101
                State.dropped_packets += diff
                print(f"\n[Warning] Lost {diff} packets (Jumped {expected} -> {packet_num})")

        State.last_packet_global = packet_num

        active_gpio = State.meta["channel0_gpio"] if channel_idx == 0 else State.meta["channel1_gpio"]

        # Timestamp generation via sample index to avoid float drift
        if channel_idx == 0:
            for adc in adc_samples:
                timestamp = int(State.samples_ch0 * SAMPLE_INTERVAL_MS)
                State.csv_writer.writerow([timestamp, channel_idx, active_gpio, int(adc), packet_num])
                State.samples_ch0 += 1
        else:
            for adc in adc_samples:
                timestamp = int(State.samples_ch1 * SAMPLE_INTERVAL_MS)
                State.csv_writer.writerow([timestamp, channel_idx, active_gpio, int(adc), packet_num])
                State.samples_ch1 += 1

    except Exception as e:
        print(f"\n[Packet Decode Error] {e}")

def on_error(ws: websocket.WebSocketApp, error: Exception) -> None:
    print(f"\n[WebSocket Error] {error}")
    State.stop_requested = True

def on_close(ws: websocket.WebSocketApp, close_status_code: int, close_msg: str) -> None:
    State.stop_requested = True

# --- CLI Interactions ---
def get_input(prompt: str, default: str) -> str:
    user_in = input(f"{prompt} [{default}]: ").strip()
    return user_in if user_in else default

def select_gpio(channel_idx: int, default_gpio: int) -> int:
    options = list(GPIO_PROTOCOL_MAP.keys())
    print(f"\nSelect GPIO for Channel {channel_idx}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}) GPIO{opt}")
    
    while True:
        val = input(f"Select option 1-{len(options)} [Default GPIO{default_gpio}]: ").strip()
        if not val:
            return default_gpio
        try:
            idx = int(val) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print(f"Invalid selection. Please choose 1-{len(options)}.")

def setup_session() -> Dict[str, Any]:
    print("====================================================")
    print("                 EEG DATA RECORDER")
    print("====================================================")
    
    meta = {}
    meta["board"] = "ESP32"
    meta["firmware"] = "BioAmp Recorder"
    meta["sampling_rate"] = int(FS)
    
    ip = get_input("ESP32 IP", "192.168.0.123")
    meta["ip"] = ip
    
    meta["subject"] = get_input("Subject Name", "Unknown")
    
    print("\nTasks:")
    for k, v in TASKS.items():
        print(f"  {k}) {v}")
    
    task_idx = get_input("Task", "1")
    if task_idx == "11":
        meta["task"] = get_input("Task Name", "Custom Task")
    else:
        meta["task"] = TASKS.get(task_idx, "Unknown Task")
        
    try:
        meta["duration_seconds"] = int(get_input("Recording Duration (seconds)", "120"))
    except ValueError:
        meta["duration_seconds"] = 120
        
    meta["channel0_gpio"] = select_gpio(0, 32)
    meta["channel1_gpio"] = select_gpio(1, 33)

    print("\nElectrodes:")
    meta["channel0_electrode"] = get_input("Channel 0 Electrode", "O1")
    meta["channel1_electrode"] = get_input("Channel 1 Electrode", "O2")
    meta["reference"] = get_input("Reference", "Fpz")
    
    meta["session_notes"] = input("\nSession Notes (optional): ").strip()
    
    print("\n===================================")
    print(f"Subject         {meta['subject']}")
    print(f"Task            {meta['task']}")
    print(f"Duration        {meta['duration_seconds']} s")
    print(f"Sampling Rate   {meta['sampling_rate']}")
    print(f"Channel 0       GPIO{meta['channel0_gpio']} -> {meta['channel0_electrode']}")
    print(f"Channel 1       GPIO{meta['channel1_gpio']} -> {meta['channel1_electrode']}")
    print(f"Reference       {meta['reference']}")
    if meta["session_notes"]:
        print(f"Notes           {meta['session_notes']}")
    print("===================================")
        
    input("\nPress ENTER to begin...")
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
    """Handle CTRL+C gracefully."""
    print("\n[INFO] CTRL+C detected. Stopping recording gracefully...")
    State.stop_requested = True

# --- Main Execution ---
def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    State.meta = setup_session()
    
    # Countdown
    for i in range(5, 0, -1):
        print(f"Starting in {i}...", end="\r")
        time.sleep(1)
    print("Starting in 0... GO!\n")
    
    folder = create_directory(State.meta)
    
    start_dt = datetime.now()
    State.meta["date"] = start_dt.strftime("%Y-%m-%d")
    State.meta["start_time"] = start_dt.strftime("%H:%M:%S")
    
    csv_path = folder / "recording.csv"
    meta_path = folder / "metadata.json"
    log_path = folder / "recording_log.txt"
    
    State.csv_file = open(csv_path, mode='w', newline='')
    State.csv_writer = csv.writer(State.csv_file)
    State.csv_writer.writerow(["timestamp_ms", "channel", "gpio", "adc", "packet"])
    
    ws_url = f"ws://{State.meta['ip']}:81/"
    print(f"Connecting to {ws_url}")
    
    State.ws_app = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    
    ws_thread = threading.Thread(target=State.ws_app.run_forever, daemon=True)
    ws_thread.start()
    
    State.is_recording = True
    duration = State.meta["duration_seconds"]
    
    try:
        while not State.stop_requested:
            if State.first_packet_time is not None:
                elapsed = time.time() - State.first_packet_time
                remaining = max(0.0, duration - elapsed)
                
                total_samples = State.samples_ch0 + State.samples_ch1
                eff_rate = (total_samples / 2.0) / elapsed if elapsed > 0 else 0.0
                
                total_expected = State.packets_received + State.dropped_packets
                loss_pct = (State.dropped_packets / total_expected * 100) if total_expected > 0 else 0.0
                
                stats = (
                    f"Elapsed: {elapsed:.1f}s | Rem: {remaining:.1f}s | "
                    f"Pkts: {State.packets_received} | "
                    f"CH0: {State.samples_ch0} | CH1: {State.samples_ch1} | "
                    f"Spl/Pkt: {State.samples_per_packet} | "
                    f"Rate: {eff_rate:.1f} Hz | "
                    f"Loss: {loss_pct:.1f}% | "
                    f"Dir: {folder.name}"
                )
                print(f"\r{stats}", end="")
                sys.stdout.flush()
                
                if elapsed >= duration:
                    print("\n\n[INFO] Time duration reached.")
                    break
            else:
                print("\rWaiting for stream to begin...", end="")
                sys.stdout.flush()
                
            time.sleep(1)
            
    except Exception as e:
        print(f"\n[Error] Main loop interrupted: {e}")
    finally:
        State.stop_requested = True
        State.is_recording = False
        if State.ws_app:
            State.ws_app.close()
            
        if State.csv_file:
            State.csv_file.close()
            
        end_dt = datetime.now()
        
        # Calculate Final Stats
        if State.first_packet_time is not None:
            actual_duration = time.time() - State.first_packet_time
        else:
            actual_duration = 0.0
            
        total_s = State.samples_ch0 + State.samples_ch1
        avg_rate = (total_s / 2.0) / actual_duration if actual_duration > 0 else 0.0
        
        total_expected = State.packets_received + State.dropped_packets
        final_loss_pct = (State.dropped_packets / total_expected * 100) if total_expected > 0 else 0.0
        
        # Write metadata.json
        meta_to_save = State.meta.copy()
        if "ip" in meta_to_save:
            del meta_to_save["ip"]
        with open(meta_path, "w") as f:
            json.dump(meta_to_save, f, indent=4)
            
        # Write recording_log.txt
        log_content = (
            f"Recording Started\n{State.meta['start_time']}\n\n"
            f"Recording Ended\n{end_dt.strftime('%H:%M:%S')}\n\n"
            f"Recording Duration\n{actual_duration:.1f} seconds\n\n"
            f"Channel 0 GPIO\n{State.meta['channel0_gpio']}\n\n"
            f"Channel 1 GPIO\n{State.meta['channel1_gpio']}\n\n"
            f"Packets Received\n{State.packets_received}\n\n"
            f"Samples CH0\n{State.samples_ch0}\n\n"
            f"Samples CH1\n{State.samples_ch1}\n\n"
            f"Samples Per Packet\n{State.samples_per_packet}\n\n"
            f"Dropped Packets\n{State.dropped_packets}\n\n"
            f"Packet Loss %\n{final_loss_pct:.2f}%\n\n"
            f"Effective Sampling Rate\n{avg_rate:.2f} Hz\n"
        )
        with open(log_path, "w") as f:
            f.write(log_content)
            
        print(f"\nRecording saved successfully to:\n{folder.resolve()}")

if __name__ == "__main__":
    main()