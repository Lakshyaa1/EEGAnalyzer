/**
 * esp32-eeg-ap-dashboard-v2.ino
 *
 * BioAmp EXG Pill  →  ESP32  →  Self-hosted WiFi AP  →  Browser dashboard
 *
 * ═══════════════════════════════════════════════════════════════════════════
 *  ROOT CAUSE OF v1 FREEZE — two bugs working together:
 *
 *  Bug 1 — handleStream() was a BLOCKING while-loop that held loop() hostage
 *  for the entire logging duration. It called webSocket.loop() inside, but it
 *  never called server.handleClient(), so any new HTTP request to port 80 hung
 *  indefinitely — including the browser's internal requests while handling the
 *  download response.
 *
 *  Bug 2 — startLogging() clicked <a href="/stream?dur=X"> WITHOUT a 'download'
 *  attribute. Most browsers treat this as a page navigation, unloading the
 *  dashboard and killing the WebSocket. Even browsers that handled
 *  Content-Disposition:attachment and stayed on the page still issued an HTTP
 *  GET that then hit Bug 1 above.
 *
 *  Result: WebSocket dies → live graph freezes → browser eventually times out
 *  trying to talk to the ESP32 which is stuck in handleStream().
 *
 * ═══════════════════════════════════════════════════════════════════════════
 *  FIX — three changes that work together:
 *
 *  1. /stream HTTP endpoint REMOVED entirely. No blocking code anywhere.
 *  2. ALL data (live graph + CSV log) flows over the WebSocket on port 81.
 *     loop() is never blocked — DNS, HTTP, and WebSocket are always serviced.
 *  3. Browser accumulates CSV text batches in a JS array, then triggers a
 *     local Blob download when logging ends — no page navigation, no HTTP.
 *
 * ═══════════════════════════════════════════════════════════════════════════
 *  WebSocket protocol (all plain-text frames):
 *
 *    ESP32 → Browser:
 *      D[{"v":RAW,"t":MS},...]   live graph batch    (always, every ~40 ms)
 *      L<raw CSV lines>          log data batch      (only while logging, ~200 ms)
 *      S:LOG_STARTED:<file>:<durS>
 *      S:LOG_COMPLETE:<file>:<sampleCount>
 *      S:LOG_STOPPED:<file>:<sampleCount>
 *      S:LOG_ERROR:<reason>
 *
 *    Browser → ESP32:
 *      C:START:<durationSeconds>
 *      C:STOP
 *
 * ═══════════════════════════════════════════════════════════════════════════
 *  Hardware (unchanged from v1):
 *    BioAmp EXG Pill OUT  →  ESP32 GPIO39 (VN)  [ADC1 — unaffected by WiFi]
 *    Onboard LED          →  GPIO 2
 *
 *  Library (unchanged from v1):
 *    "WebSockets" by Markus Sattler  (Arduino Library Manager)
 *    Everything else is in the ESP32 Arduino core.
 *
 *  Partition scheme: any scheme works — no filesystem used at all.
 *  (No LittleFS, no SPIFFS, nothing. CSV lives in browser RAM until download.)
 */

#include <WiFi.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <WebSocketsServer.h>
#include <Preferences.h>

// ═══════════════════════════════════════════════════════
//  USER SETTINGS  — only things you might want to change
// ═══════════════════════════════════════════════════════
#define AP_SSID              "EEG-Sensor"    // WiFi name seen by phone/laptop
#define AP_PASS              "eeg123456"     // WPA2 password (8+ chars)
#define SENSOR_NAME          "BioAmpEXG"    // used in the downloaded filename

#define ADC_PIN_CH0          32             // Channel 0 input
#define ADC_PIN_CH1          33             // Channel 1 input
#define ONBOARD_LED          2

#define SAMPLE_RATE_HZ       250            // 250 Hz is ideal for EXG/EEG
#define MAX_LOG_DURATION_S   28800          // 8-hour hard safety cap
// ═══════════════════════════════════════════════════════

#define SAMPLE_INTERVAL_MS  (1000 / SAMPLE_RATE_HZ)
#define WS_PORT             81
#define HTTP_PORT           80
#define DNS_PORT            53

// 4-second deep queue so loop() jitter never drops a sample.
// x2 because each tick now pushes one sample per channel (CH0 + CH1).
#define QUEUE_DEPTH         (SAMPLE_RATE_HZ * 4 * 2)

// ── Live graph WS batch ─────────────────────────────────────────
// Flushed every GRAPH_FLUSH_MS ms OR every GRAPH_FLUSH_N samples,
// whichever comes first. Keeps the scrolling graph visually smooth.
#define GRAPH_BUF_SIZE      4096
#define GRAPH_FLUSH_MS      40      // ~25 fps graph refresh
#define GRAPH_FLUSH_N       10      // at most 10 samples per frame

// ── Log CSV WS batch ────────────────────────────────────────────
// Flushed every LOG_FLUSH_MS ms or when the buffer is nearly full.
// Each flush is one WS frame the browser appends to its CSV array.
// At 250 Hz, 200 ms flush = 50 lines per frame = ~1250 bytes/frame.
#define LOG_BUF_SIZE        8192
#define LOG_LINE_MAX        50      // max bytes per CSV line
#define LOG_FLUSH_MS        200

// ──────────────────────────────────────────────────────────────────
WebServer        server(HTTP_PORT);
WebSocketsServer webSocket(WS_PORT);
DNSServer        dnsServer;
Preferences      prefs;

typedef struct { uint16_t value; uint32_t timestamp; uint8_t channel; uint8_t gpio; } Sample_t;

// Per-channel running sample index — used as the CSV "packet" column so the
// existing eegrecorder.py CSV schema doesn't have to change. NOTE: this is
// NOT a real packet-loss indicator (there are no fixed-size binary frames
// on this transport) — it's just a monotonic per-channel counter.
uint32_t sampleIdxCh0 = 0;
uint32_t sampleIdxCh1 = 0;

QueueHandle_t sampleQueue   = NULL;
TaskHandle_t  adcTaskHandle = NULL;

// ── Logging session state ──────────────────────────────────────
bool     isLogging      = false;
bool     stopRequested  = false;
uint32_t logStartMs     = 0;
uint32_t logDurationMs  = 0;
uint32_t logSampleNo    = 0;
char     logFilename[72];

// ── Graph WS batch ─────────────────────────────────────────────
char     graphBuf[GRAPH_BUF_SIZE];
int      graphBufPos    = 0;
int      graphBufCount  = 0;
uint32_t graphLastFlush = 0;

// ── Log CSV WS batch ───────────────────────────────────────────
char     logBuf[LOG_BUF_SIZE];
int      logBufPos      = 0;
uint32_t logLastFlush   = 0;


// ══════════════════════════════════════════════════════════════
//  DASHBOARD HTML
//  Fully self-contained — no external CDN, works 100% offline.
// ══════════════════════════════════════════════════════════════
static const char* DASHBOARD_HTML = R"HTMLPAGE(
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EEG Live Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Roboto,sans-serif;background:#0f172a;color:#e2e8f0;
     padding:1rem;min-height:100vh}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:1.3rem;color:#38bdf8;margin-bottom:.2rem}
.sub{font-size:.78rem;color:#64748b;margin-bottom:.8rem}
.statusRow{display:flex;align-items:center;gap:.5rem;margin-bottom:.8rem;
           font-size:.8rem;color:#94a3b8}
.dot{width:9px;height:9px;border-radius:50%;background:#ef4444;
     display:inline-block;transition:background .3s}
.dot.on{background:#4ade80;box-shadow:0 0 8px #4ade80}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;
      padding:1.2rem;margin-bottom:.8rem}
.bigval{font-size:2.6rem;font-weight:700;color:#38bdf8;line-height:1}
.bigval span{font-size:1rem;color:#64748b;font-weight:400}
.metrics{display:flex;gap:1.5rem;margin-top:.5rem;font-size:.8rem;color:#94a3b8}
.metrics b{color:#e2e8f0}
canvas{width:100%;height:220px;background:#0f172a;border-radius:10px;
       border:1px solid #334155;display:block}
.logRow{display:flex;gap:.6rem;flex-wrap:wrap;align-items:flex-end;margin-top:.8rem}
.field{display:flex;flex-direction:column;gap:.3rem}
label{font-size:.72rem;font-weight:600;color:#94a3b8;
      text-transform:uppercase;letter-spacing:.04em}
input,select{padding:.55rem .7rem;background:#0f172a;border:1px solid #475569;
             border-radius:8px;color:#e2e8f0;font-size:.9rem;outline:none}
input:focus,select:focus{border-color:#38bdf8}
input[type=number]{width:110px}
button{padding:.6rem 1.1rem;border:none;border-radius:8px;font-weight:700;
       cursor:pointer;font-size:.88rem;transition:opacity .15s}
button:hover:not(:disabled){opacity:.82}
button:disabled{opacity:.38;cursor:not-allowed}
.btnStart{background:linear-gradient(135deg,#0ea5e9,#6366f1);color:#fff}
.btnStop{background:#ef4444;color:#fff}
.logStatus{margin-top:.9rem;font-size:.84rem;color:#94a3b8}
.logStatus b{color:#38bdf8}
.progressTrack{height:7px;background:#0f172a;border-radius:99px;margin-top:.5rem;
               overflow:hidden;border:1px solid #334155}
.progressFill{height:100%;background:linear-gradient(90deg,#0ea5e9,#6366f1);
              width:0%;transition:width .3s linear}
.memRow{display:flex;align-items:center;gap:.5rem;margin-top:.35rem;
        font-size:.7rem;color:#475569}
.memTrack{flex:1;height:4px;background:#0f172a;border-radius:99px;
          overflow:hidden;border:1px solid #1e3a5f}
.memFill{height:100%;background:#6366f1;width:0%;transition:width .5s}
.footnote{font-size:.7rem;color:#475569;margin-top:.5rem;line-height:1.6}
</style>
</head>
<body>
<div class="wrap">
  <h1>&#9889; EEG / EXG Live Dashboard</h1>
  <div class="sub" id="sensorLabel">Sensor: ...</div>

  <div class="statusRow">
    <span class="dot" id="connDot"></span>
    <span id="connText">Connecting&#8230;</span>
    <span style="margin-left:auto" id="rateText"></span>
  </div>

  <div class="card">
    <div class="bigval"><span id="bigValNum">--</span> <span>raw ADC</span></div>
    <div class="metrics">
      <div>Voltage:&nbsp;<b id="voltVal">-- V</b></div>
      <div>Total received:&nbsp;<b id="totalSamples">0</b></div>
    </div>
  </div>

  <div class="card" style="padding:.8rem 1.2rem">
    <canvas id="graph"></canvas>
  </div>

  <div class="card">
    <label style="display:block;margin-bottom:.6rem">Data Logging</label>
    <div class="logRow">
      <div class="field">
        <label>Duration</label>
        <input id="durVal" type="number" min="1" max="28800" value="60">
      </div>
      <div class="field">
        <label>Unit</label>
        <select id="durUnit">
          <option value="1">Seconds</option>
          <option value="60">Minutes</option>
          <option value="3600">Hours</option>
        </select>
      </div>
      <button class="btnStart" id="startBtn" onclick="startLogging()">&#9654; Start Logging</button>
      <button class="btnStop"  id="stopBtn"  onclick="stopLogging()" disabled>&#9632; Stop</button>
    </div>

    <div class="logStatus" id="logStatus">Not logging.</div>
    <div class="progressTrack"><div class="progressFill" id="progressFill"></div></div>
    <div class="memRow">
      <span id="memLabel">Browser CSV buffer: 0 KB</span>
      <div class="memTrack"><div class="memFill" id="memFill"></div></div>
    </div>
    <div class="footnote">
      CSV data is streamed to this browser over WebSocket and downloaded
      automatically when the session ends. Nothing is stored on the ESP32.
      The live graph stays fully active throughout logging.
    </div>
  </div>
</div>

<script>
// ── WebSocket state ────────────────────────────────────────────
let ws, connected = false;

// ── Live graph ─────────────────────────────────────────────────
let graphBuf = [];
const MAX_POINTS = 750;       // ~3 s of history at 250 Hz

// ── Sample rate readout ────────────────────────────────────────
let totalSamples = 0, rateCount = 0, lastRateTime = Date.now();

// ── Logging state ──────────────────────────────────────────────
let logging = false, logEndsAt = 0, logTotalMs = 0;
let csvBatches = [];          // array of CSV text strings accumulated from 'L' frames
let progressTimer = null;
let currentFilename = '';
let csvBytesTotal = 0;
const MEM_WARN_BYTES = 180 * 1024 * 1024; // 180 MB (8 h × 250 Hz × ~25 B)

// ── Connect / reconnect ────────────────────────────────────────
function connectWS() {
  ws = new WebSocket('ws://' + location.hostname + ':81/');
  ws.onopen  = () => { connected = true;  setDot(true);  };
  ws.onclose = () => { connected = false; setDot(false); setTimeout(connectWS, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = ({ data }) => handleWS(data);
}

function setDot(on) {
  document.getElementById('connDot').classList.toggle('on', on);
  document.getElementById('connText').innerText =
    on ? 'Connected' : 'Disconnected \u2014 retrying\u2026';
}

// ── Main WS message dispatcher ─────────────────────────────────
function handleWS(data) {
  if (!data || !data.length) return;
  const pfx = data[0];

  // ── 'D' = live graph batch: D[{"v":RAW,"t":MS},...] ──────────
  if (pfx === 'D') {
    try {
      const arr = JSON.parse(data.slice(1));
      for (const s of arr) {
        graphBuf.push(s);
        totalSamples++;
        rateCount++;
      }
      if (graphBuf.length > MAX_POINTS)
        graphBuf.splice(0, graphBuf.length - MAX_POINTS);
      if (arr.length) {
        const last = arr[arr.length - 1];
        document.getElementById('bigValNum').innerText = last.v;
        document.getElementById('voltVal').innerText =
          (last.v * 3.3 / 4095).toFixed(3) + ' V';
      }
      document.getElementById('totalSamples').innerText =
        totalSamples.toLocaleString();
    } catch(e) {}

  // ── 'L' = log CSV batch (raw CSV text after the 'L' prefix) ──
  } else if (pfx === 'L') {
    if (logging) {
      const chunk = data.slice(1);
      csvBatches.push(chunk);
      csvBytesTotal += chunk.length;
      updateMemBar();
    }

  // ── 'S' = status message from ESP32 ──────────────────────────
  } else if (pfx === 'S') {
    handleStatus(data.slice(2));   // strip leading "S:"
  }
}

// ── Handle ESP32 status messages ──────────────────────────────
function handleStatus(payload) {
  const parts = payload.split(':');
  const type  = parts[0];

  if (type === 'LOG_STARTED') {
    currentFilename = parts[1];
    logTotalMs  = parseInt(parts[2]) * 1000;
    logEndsAt   = Date.now() + logTotalMs;
    logging     = true;
    csvBatches  = ['timestamp_ms,channel,gpio,adc,packet\n'];  // CSV header first
    csvBytesTotal = csvBatches[0].length;
    document.getElementById('startBtn').disabled = true;
    document.getElementById('stopBtn').disabled  = false;
    document.getElementById('logStatus').innerHTML =
      'Logging to <b>' + currentFilename + '</b>\u2026';
    if (progressTimer) clearInterval(progressTimer);
    progressTimer = setInterval(tickProgress, 250);

  } else if (type === 'LOG_COMPLETE' || type === 'LOG_STOPPED') {
    const fname  = parts[1];
    const count  = parseInt(parts[2]);
    const manual = type === 'LOG_STOPPED';
    logging = false;
    if (progressTimer) clearInterval(progressTimer);
    document.getElementById('progressFill').style.width = manual ? '' : '100%';
    document.getElementById('startBtn').disabled = false;
    document.getElementById('stopBtn').disabled  = true;
    document.getElementById('logStatus').innerHTML =
      (manual ? '\u25a0 Stopped: ' : '\u2713 Complete: ') +
      '<b>' + fname + '</b> \u2014 ' +
      count.toLocaleString() + ' samples. Downloading\u2026';
    triggerDownload(fname);
    setTimeout(() => {
      document.getElementById('progressFill').style.width = '0%';
      document.getElementById('memFill').style.width = '0%';
      document.getElementById('memLabel').innerText = 'Browser CSV buffer: 0 KB';
    }, 4000);

  } else if (type === 'LOG_ERROR') {
    logging = false;
    if (progressTimer) clearInterval(progressTimer);
    document.getElementById('startBtn').disabled = false;
    document.getElementById('stopBtn').disabled  = true;
    document.getElementById('logStatus').innerText =
      '\u26a0 Error: ' + parts.slice(1).join(':');
  }
}

// ── Progress bar & timer ───────────────────────────────────────
function tickProgress() {
  const rem = Math.max(0, logEndsAt - Date.now());
  const pct = Math.min(100, 100 * (1 - rem / logTotalMs));
  document.getElementById('progressFill').style.width = pct + '%';
  document.getElementById('logStatus').innerHTML =
    'Logging to <b>' + currentFilename + '</b> \u2014 ' +
    Math.ceil(rem / 1000) + 's remaining';
}

// ── Browser RAM usage bar (purple strip under progress bar) ───
function updateMemBar() {
  const pct = Math.min(100, (csvBytesTotal / MEM_WARN_BYTES) * 100);
  document.getElementById('memFill').style.width = pct + '%';
  const kb = (csvBytesTotal / 1024).toFixed(0);
  const mb = (csvBytesTotal / 1048576).toFixed(1);
  document.getElementById('memLabel').innerText =
    'Browser CSV buffer: ' + (csvBytesTotal > 1048576 ? mb + ' MB' : kb + ' KB');
}

// ── Download the accumulated CSV as a local Blob file ─────────
// This is the KEY fix: no HTTP navigation, no /stream endpoint.
// The browser creates a download link from data already in RAM.
function triggerDownload(filename) {
  try {
    const blob = new Blob(csvBatches, { type: 'text/csv' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = filename;         // 'download' attr = save file, never navigate
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 15000);
    csvBatches    = [];
    csvBytesTotal = 0;
  } catch(e) {
    document.getElementById('logStatus').innerText =
      '\u26a0 Download failed: ' + e.message;
  }
}

// ── Logging control ────────────────────────────────────────────
function startLogging() {
  if (!connected) { alert('Not connected to ESP32.'); return; }
  const val  = parseInt(document.getElementById('durVal').value);
  const unit = parseInt(document.getElementById('durUnit').value);
  if (!val || val <= 0) { alert('Enter a valid duration.'); return; }
  // Send command over existing WebSocket — no new HTTP request, no navigation
  ws.send('C:START:' + (val * unit));
}

function stopLogging() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send('C:STOP');
}

// ── Canvas live graph ─────────────────────────────────────────
const canvas = document.getElementById('graph');
const ctx    = canvas.getContext('2d');

function resizeCanvas() {
  canvas.width  = canvas.clientWidth  * devicePixelRatio;
  canvas.height = canvas.clientHeight * devicePixelRatio;
}
window.addEventListener('resize', resizeCanvas);
resizeCanvas();

function drawGraph() {
  requestAnimationFrame(drawGraph);
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (graphBuf.length < 2) return;

  let mn = Infinity, mx = -Infinity;
  for (const s of graphBuf) {
    if (s.v < mn) mn = s.v;
    if (s.v > mx) mx = s.v;
  }
  if (mn === mx) { mn -= 20; mx += 20; }
  const pad = (mx - mn) * 0.08;
  mn -= pad; mx += pad;

  // Grid lines
  ctx.strokeStyle = '#1e3a5f'; ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = (h / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  // Y-axis labels
  ctx.fillStyle = '#334155';
  ctx.font = (10 * devicePixelRatio) + 'px sans-serif';
  ctx.fillText(Math.round(mx), 4, 13 * devicePixelRatio);
  ctx.fillText(Math.round(mn), 4, h - 4);

  // Signal line
  ctx.strokeStyle = '#38bdf8';
  ctx.lineWidth   = 1.8 * devicePixelRatio;
  ctx.lineJoin    = 'round';
  ctx.beginPath();
  for (let i = 0; i < graphBuf.length; i++) {
    const x = (i / (MAX_POINTS - 1)) * w;
    const y = h - ((graphBuf[i].v - mn) / (mx - mn)) * h;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();
}
drawGraph();

// ── Sample-rate readout (updates every second) ─────────────────
setInterval(() => {
  const dt = (Date.now() - lastRateTime) / 1000;
  document.getElementById('rateText').innerText =
    (rateCount / dt).toFixed(0) + ' Hz';
  rateCount = 0;
  lastRateTime = Date.now();
}, 1000);

document.getElementById('sensorLabel').innerText = 'Sensor: )HTMLPAGE" SENSOR_NAME R"HTMLPAGE( (CH0: GPIO32, CH1: GPIO33)';
connectWS();
</script>
</body>
</html>
)HTMLPAGE";


// ══════════════════════════════════════════════════════════════
//  Graph WS batch helpers
// ══════════════════════════════════════════════════════════════

void initGraphBatch() {
  graphBufPos   = 0;
  graphBufCount = 0;
  graphBuf[graphBufPos++] = '[';
  graphLastFlush = millis();
}

void graphBatchAdd(const Sample_t& s) {
  char tmp[40];
  int len = snprintf(tmp, sizeof(tmp), "%s{\"v\":%u,\"t\":%lu,\"c\":%u}",
                     graphBufCount > 0 ? "," : "",
                     s.value, (unsigned long)s.timestamp, s.channel);
  if (len > 0 && graphBufPos + len < GRAPH_BUF_SIZE - 4) {
    memcpy(graphBuf + graphBufPos, tmp, len);
    graphBufPos  += len;
    graphBufCount++;
  }
}

void flushGraphBatch() {
  if (graphBufCount == 0) return;
  graphBuf[graphBufPos++] = ']';
  graphBuf[graphBufPos]   = '\0';

  // Prefix 'D' tells the browser JS this is a data (graph) packet
  static char outMsg[GRAPH_BUF_SIZE + 2];
  outMsg[0] = 'D';
  memcpy(outMsg + 1, graphBuf, graphBufPos + 1);
  webSocket.broadcastTXT(outMsg, graphBufPos + 1);

  initGraphBatch();
}


// ══════════════════════════════════════════════════════════════
//  Log CSV WS batch helpers
// ══════════════════════════════════════════════════════════════

void initLogBatch() {
  logBufPos     = 0;
  logLastFlush  = millis();
}

// Append one CSV line to the log batch. Called from loop() when logging.
// Columns match eegrecorder.py's existing schema exactly:
//   timestamp_ms, channel, gpio, adc, packet
// "packet" here is a per-channel running sample index, NOT a real frame
// number (this transport has no fixed-size binary packets) — kept only
// for column-compatibility with the analysis script.
void logBatchAdd(const Sample_t& s) {
  uint32_t pseudoPacket = (s.channel == 0) ? sampleIdxCh0++ : sampleIdxCh1++;
  int len = snprintf(logBuf + logBufPos, LOG_BUF_SIZE - logBufPos,
                     "%lu,%u,%u,%u,%lu\n",
                     (unsigned long)s.timestamp,
                     s.channel,
                     s.gpio,
                     s.value,
                     (unsigned long)pseudoPacket);
  if (len > 0) logBufPos += len;
}

// Flush accumulated CSV lines to browser via WS.
// Prefixed with 'L' so the browser JS knows to append to csvBatches[].
void flushLogBatch() {
  if (logBufPos == 0) return;

  // Prefix 'L' tells the browser JS this is log CSV data
  static char outMsg[LOG_BUF_SIZE + 2];
  outMsg[0] = 'L';
  memcpy(outMsg + 1, logBuf, logBufPos);
  outMsg[logBufPos + 1] = '\0';
  webSocket.broadcastTXT(outMsg, logBufPos + 1);

  initLogBatch();
}


// ══════════════════════════════════════════════════════════════
//  HTTP handlers
// ══════════════════════════════════════════════════════════════

void handleRoot() {
  server.send(200, "text/html", DASHBOARD_HTML);
}

// Catches every captive-portal probe URL (Apple /hotspot-detect.html,
// Android /generate_204, Windows /ncsi.txt, etc.) and redirects to the
// dashboard. This is what triggers the "Sign in to network" popup on
// phones and laptops the moment they join the AP.
void handleCaptive() {
  server.sendHeader("Location", "http://192.168.4.1/", true);
  server.send(302, "text/plain", "");
}


// ══════════════════════════════════════════════════════════════
//  WebSocket event handler
//  Receives control commands from the browser.
// ══════════════════════════════════════════════════════════════
void webSocketEvent(uint8_t num, WStype_t type,
                    uint8_t* payload, size_t length) {
  if (type != WStype_TEXT || length == 0) return;
  String msg = String((char*)payload).substring(0, length);

  if (msg.startsWith("C:START:") && !isLogging) {
    uint32_t durationSec = (uint32_t)msg.substring(8).toInt();

    if (durationSec == 0 || durationSec > MAX_LOG_DURATION_S) {
      webSocket.sendTXT(num,
        "S:LOG_ERROR:Duration out of range (1 – " + String(MAX_LOG_DURATION_S) + "s)");
      return;
    }

    // Increment persistent run counter (survives reboots)
    prefs.begin("eeglog", false);
    uint32_t counter = prefs.getUInt("ctr", 0) + 1;
    prefs.putUInt("ctr", counter);
    prefs.end();

    snprintf(logFilename, sizeof(logFilename),
             "EEG_%s_Run%03lu_%lus.csv",
             SENSOR_NAME,
             (unsigned long)counter,
             (unsigned long)durationSec);

    logSampleNo   = 0;
    sampleIdxCh0  = 0;
    sampleIdxCh1  = 0;
    logDurationMs = durationSec * 1000UL;
    logStartMs    = millis();
    stopRequested = false;
    initLogBatch();
    isLogging     = true;

    char wsMsg[96];
    snprintf(wsMsg, sizeof(wsMsg), "S:LOG_STARTED:%s:%lu",
             logFilename, (unsigned long)durationSec);
    webSocket.broadcastTXT(wsMsg);

    digitalWrite(ONBOARD_LED, HIGH);
    Serial.printf("[Log] Started: %s  (%lu s)\n",
                  logFilename, (unsigned long)durationSec);

  } else if (msg == "C:STOP" && isLogging) {
    stopRequested = true;
  }
}


// ══════════════════════════════════════════════════════════════
//  Finish a logging session (called from loop())
// ══════════════════════════════════════════════════════════════
void finishLogging(bool timerExpired) {
  isLogging = false;

  // Flush whatever CSV is still in the buffer
  flushLogBatch();

  char wsMsg[96];
  snprintf(wsMsg, sizeof(wsMsg), "S:%s:%s:%lu",
           timerExpired ? "LOG_COMPLETE" : "LOG_STOPPED",
           logFilename,
           (unsigned long)logSampleNo);
  webSocket.broadcastTXT(wsMsg);

  digitalWrite(ONBOARD_LED, LOW);
  Serial.printf("[Log] %s — %s — %lu samples\n",
                timerExpired ? "Complete" : "Stopped",
                logFilename,
                (unsigned long)logSampleNo);
}


// ══════════════════════════════════════════════════════════════
//  ADC sampling task — Core 0, high priority, precise timing
//  Completely isolated from WiFi/HTTP/WS on Core 1.
// ══════════════════════════════════════════════════════════════
void adcSampleTask(void* parameter) {
  TickType_t lastWake = xTaskGetTickCount();
  for (;;) {
    vTaskDelayUntil(&lastWake, pdMS_TO_TICKS(SAMPLE_INTERVAL_MS));
    uint32_t now = millis();

    Sample_t s0;
    s0.value     = (uint16_t)analogRead(ADC_PIN_CH0);
    s0.timestamp = now;
    s0.channel   = 0;
    s0.gpio      = ADC_PIN_CH0;
    xQueueSend(sampleQueue, &s0, 0);   // never block — drop if queue full

    Sample_t s1;
    s1.value     = (uint16_t)analogRead(ADC_PIN_CH1);
    s1.timestamp = now;
    s1.channel   = 1;
    s1.gpio      = ADC_PIN_CH1;
    xQueueSend(sampleQueue, &s1, 0);
  }
}


// ══════════════════════════════════════════════════════════════
//  setup()
// ══════════════════════════════════════════════════════════════
void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(ONBOARD_LED, OUTPUT);
  digitalWrite(ONBOARD_LED, LOW);

  // ADC: 12-bit, 0–3.3 V (11 dB attenuation), both channels
  analogReadResolution(12);
  analogSetPinAttenuation(ADC_PIN_CH0, ADC_11db);
  analogSetPinAttenuation(ADC_PIN_CH1, ADC_11db);
  pinMode(ADC_PIN_CH0, INPUT);
  pinMode(ADC_PIN_CH1, INPUT);

  // ── ADC self-test: gives you instant wiring feedback on Serial Monitor ─
  int testPins[2] = { ADC_PIN_CH0, ADC_PIN_CH1 };
  for (int ch = 0; ch < 2; ch++) {
    Serial.printf("[ADC] CH%d self-test on GPIO%d (50 reads)...\n", ch, testPins[ch]);
    uint32_t sum = 0; uint16_t lo = 4095, hi = 0;
    for (int i = 0; i < 50; i++) {
      uint16_t v = analogRead(testPins[ch]);
      sum += v; if (v < lo) lo = v; if (v > hi) hi = v;
      delay(5);
    }
    Serial.printf("[ADC] CH%d min=%u  max=%u  avg=%u  (%.3f V avg)\n",
                  ch, lo, hi, sum/50, (sum/50)*3.3f/4095.0f);
    if (hi < 10)         Serial.printf("[ADC] CH%d WARNING: all reads near 0 — check wiring/GND\n", ch);
    else if (lo == hi)   Serial.printf("[ADC] CH%d WARNING: values static — pin may be floating\n", ch);
    else                 Serial.printf("[ADC] CH%d OK\n", ch);
  }

  // ── WiFi Access Point ──────────────────────────────────────────────────
  WiFi.mode(WIFI_AP);
  WiFi.softAP(AP_SSID, AP_PASS);
  IPAddress apIP = WiFi.softAPIP();
  Serial.printf("[AP] SSID: \"%s\"  →  http://%s\n", AP_SSID, apIP.toString().c_str());

  // ── Captive portal DNS ─────────────────────────────────────────────────
  // Wildcard DNS: every domain resolves to our IP, triggering the
  // "Sign in to network" popup on phones/laptops automatically.
  dnsServer.start(DNS_PORT, "*", apIP);

  // ── HTTP routes ────────────────────────────────────────────────────────
  server.on("/",                        HTTP_GET, handleRoot);
  server.on("/generate_204",            HTTP_GET, handleCaptive);  // Android
  server.on("/hotspot-detect.html",     HTTP_GET, handleCaptive);  // Apple
  server.on("/ncsi.txt",                HTTP_GET, handleCaptive);  // Windows
  server.on("/connecttest.txt",         HTTP_GET, handleCaptive);  // Windows 10
  server.on("/redirect",                HTTP_GET, handleCaptive);
  server.onNotFound(handleCaptive);  // catch-all for any other probe
  server.begin();
  Serial.println("[HTTP] Server started on port 80");

  // ── WebSocket server ───────────────────────────────────────────────────
  webSocket.begin();
  webSocket.onEvent(webSocketEvent);
  Serial.printf("[WS] Server started on port %d\n", WS_PORT);

  // ── FreeRTOS queue + ADC task on core 0 ───────────────────────────────
  sampleQueue = xQueueCreate(QUEUE_DEPTH, sizeof(Sample_t));
  xTaskCreatePinnedToCore(adcSampleTask, "ADCSample",
                          4096, NULL, 3, &adcTaskHandle, 0);

  initGraphBatch();
  initLogBatch();

  Serial.printf("[Setup] Sampling GPIO%d (CH0) + GPIO%d (CH1) at %d Hz each. Dashboard ready.\n",
                ADC_PIN_CH0, ADC_PIN_CH1, SAMPLE_RATE_HZ);
  Serial.printf("[Setup] Connect to WiFi \"%s\" and open http://%s\n",
                AP_SSID, apIP.toString().c_str());
}


// ══════════════════════════════════════════════════════════════
//  loop()  —  Core 1, NEVER blocks
//
//  Responsibilities:
//   1. Service DNS (captive portal) and HTTP (dashboard page)
//   2. Service WebSocket (live graph + control messages)
//   3. Drain sample queue → feed graph batch and (if logging) log batch
//   4. Flush graph batch every 40 ms  (keeps live graph smooth)
//   5. Flush log CSV batch every 200 ms  (streams CSV to browser)
//   6. Detect logging duration expiry and call finishLogging()
// ══════════════════════════════════════════════════════════════
void loop() {
  dnsServer.processNextRequest();  // captive portal DNS
  server.handleClient();           // dashboard HTTP — ALWAYS serviced, never blocked
  webSocket.loop();                // live graph WS + C:START / C:STOP commands

  // ── Drain sample queue ──────────────────────────────────────
  Sample_t s;
  while (xQueueReceive(sampleQueue, &s, 0) == pdTRUE) {

    // Always add to the live-graph batch
    graphBatchAdd(s);

    // If a logging session is active, also add a CSV line to the log batch
    if (isLogging) {
      logSampleNo++;
      logBatchAdd(s);
    }
  }

  // ── Flush graph batch (every 40 ms or every 10 samples) ────
  bool graphDue = (millis() - graphLastFlush) >= GRAPH_FLUSH_MS;
  if ((graphDue || graphBufCount >= GRAPH_FLUSH_N) && graphBufCount > 0) {
    flushGraphBatch();
  }

  // ── Flush log CSV batch (every 200 ms or when near full) ───
  if (isLogging) {
    bool logDue  = (millis() - logLastFlush) >= LOG_FLUSH_MS;
    bool logFull = logBufPos > LOG_BUF_SIZE - LOG_LINE_MAX;
    if ((logDue || logFull) && logBufPos > 0) {
      flushLogBatch();
    }

    // ── Check if logging duration has elapsed ─────────────────
    bool timerDone = (millis() - logStartMs) >= logDurationMs;
    if (timerDone || stopRequested) {
      finishLogging(timerDone && !stopRequested);
    }
  }
}
