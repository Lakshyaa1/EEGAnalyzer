// EXG-Visualizer (patched firmware)
// https://github.com/upsidedownlabs/BioSignal-Recorder

// Original copyrights preserved:
// Copyright (c) 2023 Mahesh Tupe tupemahesh91@gmail.com
// Copyright (c) 2021 Moteen Shah moteenshah.02@gmail.com
// Copyright (c) 2023 Upside Down Labs - contact@upsidedownlabs.tech

// Patch notes (why this differs from upstream):
// - WStype_DISCONNECTED now frees per-channel buffers and stops/detaches the
//   corresponding hw timers. Upstream leaked both on every reconnect, so a
//   long recording session with any browser refresh/reconnect would slowly
//   exhaust heap and eventually hang or crash the board.
// - loop() now yields (vTaskDelay(1)) once per pass so the WiFi/TCP stack is
//   guaranteed scheduling time even under sustained interrupt load. Without
//   this, a tight loop() can starve background networking tasks.
// - Added periodic free-heap logging over Serial so a leak (if one creeps
//   back in) is visible during a soak test instead of surfacing only as an
//   unexplained hang after N minutes.
// - webSocket.sendBIN() is left as-is (it's fine at 256Hz data rates) but
//   note it is a BLOCKING call: if the client-side tab stops draining the
//   socket, this call can stall. That risk is addressed on the client side
//   (streaming CSV writer decoupled from chart rendering) rather than here.

#include <WebSocketsServer.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>
#include <SPIFFS.h>
#include <driver/adc.h>
#include <math.h>

const char *SSID = "NCAIR IOT";
const char *PASSWORD = "Asim@123Tewari";

AsyncWebServer server(80);
WebSocketsServer webSocket = WebSocketsServer(81);

hw_timer_t * timer_1 = NULL;
hw_timer_t * timer_2 = NULL;
hw_timer_t * timer_3 = NULL;
hw_timer_t * timer_4 = NULL;

portMUX_TYPE timerMux_1 = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE timerMux_2 = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE timerMux_3 = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE timerMux_4 = portMUX_INITIALIZER_UNLOCKED;

volatile int interruptCounter[4] = {0};

void IRAM_ATTR onTimer_1() {
  portENTER_CRITICAL_ISR(&timerMux_1);
  interruptCounter[0]++;
  portEXIT_CRITICAL_ISR(&timerMux_1);
}
void IRAM_ATTR onTimer_2() {
  portENTER_CRITICAL_ISR(&timerMux_2);
  interruptCounter[1]++;
  portEXIT_CRITICAL_ISR(&timerMux_2);
}
void IRAM_ATTR onTimer_3() {
  portENTER_CRITICAL_ISR(&timerMux_3);
  interruptCounter[2]++;
  portEXIT_CRITICAL_ISR(&timerMux_3);
}
void IRAM_ATTR onTimer_4() {
  portENTER_CRITICAL_ISR(&timerMux_4);
  interruptCounter[3]++;
  portEXIT_CRITICAL_ISR(&timerMux_4);
}

// Timer pointers/mutexes indexed by channel, used for cleanup on disconnect.
hw_timer_t ** const timers[4] = { &timer_1, &timer_2, &timer_3, &timer_4 };

void setup()
{
  Serial.begin(115200);

  if (!SPIFFS.begin()) {
    Serial.println("An Error has occurred while mounting SPIFFS");
    return;
  }

  WiFi.begin(SSID, PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(1000);
    Serial.println("Connecting to WiFi..");
  }

  Serial.println("");
  Serial.print("IP Address: ");
  Serial.println(WiFi.localIP());

  server.on("/", HTTP_GET, [](AsyncWebServerRequest * request)
  {
    request->send(SPIFFS, "/index.html");
  });

  server.begin();
  webSocket.begin();
  webSocket.onEvent(callback);
}

bool sample = false;
int sampling_rate = 0;
int adc[4];
int channel_count = 0;
int total_channel = 0;

// We will use 2D array for storing data of all 4 channels.
uint16_t **buffer_add = (uint16_t **)calloc(4, sizeof(uint16_t *));

// Frees buffers and stops timers for all channels. Called on disconnect so
// a reconnect starts from a clean slate instead of leaking heap.
void reset_channels()
{
  for (int i = 0; i < 4; i++) {
    if (*timers[i] != NULL) {
      timerAlarmDisable(*timers[i]);
      timerDetachInterrupt(*timers[i]);
      timerEnd(*timers[i]);
      *timers[i] = NULL;
    }
    if (buffer_add[i] != NULL) {
      free(buffer_add[i]);
      buffer_add[i] = NULL;
    }
    interruptCounter[i] = 0;
  }
}

void callback(byte num, WStype_t type, uint8_t * payload, size_t length)
{
  switch (type)
  {
    case WStype_DISCONNECTED:
      Serial.println("Client Disconnected");
      sample = false;
      channel_count = 0;
      total_channel = 0;
      reset_channels();
      Serial.print("Free heap after cleanup: ");
      Serial.println(ESP.getFreeHeap());
      break;

    case WStype_CONNECTED:
      Serial.println("Client connected");
      // Defensive: make sure we start clean even if the previous session
      // didn't get a clean DISCONNECTED event (e.g. power blip on client).
      reset_channels();
      channel_count = 0;
      total_channel = 0;
      sample = true;
      break;

    case WStype_TEXT:
      String rate;
      String gpio;

      gpio += (char)payload[length - 1];
      gpio += '\n';
      if (gpio.toInt() == 9)
      {
        String temp;
        temp += (char)payload[0];
        total_channel = temp.toInt();
        Serial.print("Total Channels: ");
        Serial.println(temp);
      }
      else {
        adc[channel_count] = gpio.toInt();
        Serial.print("Channel: ");
        Serial.println(gpio);

        for (int i = 0; i < length - 1; i++)
        {
          rate += (char)payload[i];
        }
        rate += '\n';
        sampling_rate = rate.toInt();
        Serial.print("Sampling rate: ");
        Serial.println(rate);

        send_samples(sampling_rate, adc[channel_count], channel_count);
        channel_count++;
      }
  }
}

void send_samples(int sampling_rate, int adc, int channel_count)
{
  int tick_count = 1000000 / sampling_rate;

  switch (channel_count)
  {
    case 0:
      timer_1 = timerBegin(0, 80, true);
      timerAttachInterrupt(timer_1, &onTimer_1, true);
      timerAlarmWrite(timer_1, tick_count, true);
      timerAlarmEnable(timer_1);
      buffer_add[0] = (uint16_t*)calloc(round((float)sampling_rate / 30.0) + 2, sizeof(uint16_t));
      break;

    case 1:
      timer_2 = timerBegin(1, 80, true);
      timerAttachInterrupt(timer_2, &onTimer_2, true);
      timerAlarmWrite(timer_2, tick_count, true);
      timerAlarmEnable(timer_2);
      buffer_add[1] = (uint16_t*)calloc(round((float)sampling_rate / 30.0) + 2, sizeof(uint16_t));
      break;

    case 2:
      timer_3 = timerBegin(2, 80, true);
      timerAttachInterrupt(timer_3, &onTimer_3, true);
      timerAlarmWrite(timer_3, tick_count, true);
      timerAlarmEnable(timer_3);
      buffer_add[2] = (uint16_t*)calloc(round((float)sampling_rate / 30.0) + 2, sizeof(uint16_t));
      break;

    case 3:
      timer_4 = timerBegin(3, 80, true);
      timerAttachInterrupt(timer_4, &onTimer_4, true);
      timerAlarmWrite(timer_4, tick_count, true);
      timerAlarmEnable(timer_4);
      buffer_add[3] = (uint16_t*)calloc(round((float)sampling_rate / 30.0) + 2, sizeof(uint16_t));
      break;
  }

  adc1_config_width(ADC_WIDTH_BIT_12);
  adc1_config_channel_atten((adc1_channel_t)(adc), ADC_ATTEN_DB_11);
}

static long packet_counter = 0;
static long buffer_counter[4] = {0};
portMUX_TYPE * timer_mux = NULL;

// Heap logging cadence for soak testing / diagnosing any future leak.
unsigned long last_heap_log = 0;
const unsigned long HEAP_LOG_INTERVAL_MS = 10000;

void loop() {
  webSocket.loop();

  for (int i = 0; i < total_channel; i++)
  {
    switch (i)
    {
      case 0: timer_mux = &timerMux_1; break;
      case 1: timer_mux = &timerMux_2; break;
      case 2: timer_mux = &timerMux_3; break;
      case 3: timer_mux = &timerMux_4; break;
    }

    if (interruptCounter[i] > 0)
    {
      portENTER_CRITICAL(timer_mux);
      interruptCounter[i]--;
      portEXIT_CRITICAL(timer_mux);

      if (buffer_counter[i] < round((float)sampling_rate / 30.0))
      {
        buffer_add[i][buffer_counter[i]] = adc1_get_raw((adc1_channel_t)adc[i]) & 0x0FFF;
        buffer_counter[i]++;
      }
      else
      {
        if (packet_counter < 100) packet_counter++;
        else packet_counter = 0;

        buffer_add[i][buffer_counter[i]] = packet_counter & 0x0FFF;
        buffer_counter[i]++;

        buffer_add[i][buffer_counter[i]] = i & 0x0FFF;
        buffer_counter[i]++;

        // NOTE: sendBIN() is blocking. At 256Hz this is not expected to be
        // an issue, but if the client ever stops draining the socket (e.g.
        // a stalled tab), this call can stall loop() along with it. The
        // client has been fixed to avoid that scenario (see index.html).
        webSocket.sendBIN(0, (uint8_t *)&buffer_add[i][0], buffer_counter[i]*sizeof(uint16_t));
        buffer_counter[i] = 0;
      }
    }
  }

  // Periodic heap logging — watch this during a long soak test. It should
  // stay flat (aside from normal small fluctuation); a steady downward
  // trend means something is still leaking.
  unsigned long now = millis();
  if (now - last_heap_log >= HEAP_LOG_INTERVAL_MS) {
    last_heap_log = now;
    Serial.print("Free heap: ");
    Serial.println(ESP.getFreeHeap());
  }

  // Yield so the WiFi/TCP background task always gets scheduling time,
  // even under sustained interrupt load from the sampling timers.
  vTaskDelay(1);
}
