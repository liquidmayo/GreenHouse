// HGA Listener Counter — LilyGo T-Display S3
//
// Polls the HGA Monitor master's /api/ticker endpoint over WiFi and shows
// the live rdio-scanner + ThinLine listener counts, HomeGrown Alerts style
// (black background, neon green numbers, status dot).
//
// Buttons: BOOT (GPIO0) = brightness levels, KEY (GPIO14) = view cycle
//          (split -> rdio zoom -> thinline zoom).

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>
#include "config.h"

// ---- board pins (LilyGo T-Display S3) ----
#define PIN_POWER_ON   15   // must be HIGH or the LCD stays dark on battery
#define PIN_LCD_BL     38
#define PIN_BTN_BRIGHT 0    // BOOT button
#define PIN_BTN_VIEW   14   // second user button

// ---- HGA palette (RGB565) ----
#define C_BG      0x0000    // black
#define C_GREEN   0x262B    // #22c55e
#define C_GREENDK 0x1326    // #166534
#define C_MUTED   0xA5F5    // #a6beac
#define C_YELLOW  0xED81    // #eab308
#define C_RED     0xEA28    // #ef4444
#define C_GRAY    0x39E7    // dim gray

#define SCREEN_W 320
#define SCREEN_H 170
#define STALE_MS 60000UL

TFT_eSPI tft;
TFT_eSprite spr(&tft);
bool useSprite = false;

// live values (-1 = unknown / not yet received)
int rdioCount = -1;
int thinlineCount = -1;
int callsMin = -1;
char lastCallTg[48] = "";         // last call talkgroup label
int lastCallAge = -1;             // seconds, as of lastOkPoll
char sysStatus[8] = "unk";        // ok | warn | crit
unsigned long lastOkPoll = 0;     // millis() of last successful poll
unsigned long lastPollAttempt = 0;
int viewMode = 0;                 // 0 split, 1 rdio zoom, 2 thinline zoom, 3 calls

const uint8_t brightLevels[] = {60, 120, 200, 255};
int brightIdx = 2;

// ---------------------------------------------------------------- display

void setBrightness(uint8_t level) {
  ledcWrite(0, level);
}

uint16_t statusColor() {
  if (strcmp(sysStatus, "crit") == 0) return C_RED;
  if (strcmp(sysStatus, "warn") == 0) return C_YELLOW;
  if (strcmp(sysStatus, "ok") == 0) return C_GREEN;
  return C_GRAY;
}

// draw a count centered at (cx, cy): FONT8 for short numbers, FONT7 longer,
// "--" via FONT4 when unknown
void drawCount(TFT_eSprite &g, int value, int cx, int cy, bool zoom) {
  g.setTextDatum(MC_DATUM);
  if (value < 0) {
    g.setTextColor(C_GRAY, C_BG);
    g.drawString("--", cx, cy, 4);
    return;
  }
  g.setTextColor(C_GREEN, C_BG);
  int digits = (value >= 1000) ? 4 : (value >= 100) ? 3 : (value >= 10) ? 2 : 1;
  int font = 8;
  if (zoom) {
    font = (digits <= 4) ? 8 : 7;
  } else {
    font = (digits <= 2) ? 8 : 7;   // half-screen cell fits 2 digits of FONT8
  }
  g.drawNumber(value, cx, cy, font);
}

void drawTopBar(TFT_eSprite &g) {
  // WiFi indicator, left
  if (WiFi.status() == WL_CONNECTED) {
    int rssi = WiFi.RSSI();
    int bars = (rssi > -55) ? 4 : (rssi > -65) ? 3 : (rssi > -75) ? 2 : 1;
    for (int i = 0; i < 4; i++) {
      int h = 3 + i * 3;
      g.fillRect(8 + i * 6, 16 - h, 4, h, (i < bars) ? C_GREEN : C_GREENDK);
    }
  } else {
    g.setTextDatum(TL_DATUM);
    g.setTextColor(C_RED, C_BG);
    g.drawString("NO WIFI", 8, 4, 2);
  }
  // title, center
  g.setTextDatum(TC_DATUM);
  g.setTextColor(C_MUTED, C_BG);
  g.drawString("HOMEGROWN ALERTS", SCREEN_W / 2, 4, 2);
  // status dot, right
  g.fillSmoothCircle(SCREEN_W - 14, 10, 6, statusColor(), C_BG);
}

void drawStaleBar(TFT_eSprite &g) {
  unsigned long age = (millis() - lastOkPoll) / 1000;
  g.setTextDatum(BC_DATUM);
  g.setTextColor(C_YELLOW, C_BG);
  char buf[32];
  snprintf(buf, sizeof(buf), "STALE %lus", age);
  g.drawString(buf, SCREEN_W / 2, SCREEN_H - 2, 2);
}

void fmtAgeShort(char *out, size_t n, int seconds) {
  if (seconds < 0) { out[0] = 0; return; }
  if (seconds < 120) snprintf(out, n, "%ds", seconds);
  else if (seconds < 5400) snprintf(out, n, "%dm", seconds / 60);
  else snprintf(out, n, "%dh", seconds / 3600);
}

// bottom strip: call rate + most recent call talkgroup
void drawCallStrip(TFT_eSprite &g) {
  if (callsMin < 0) return;
  char age[12] = "";
  if (lastCallAge >= 0 && lastOkPoll > 0) {
    fmtAgeShort(age, sizeof(age),
                lastCallAge + (int)((millis() - lastOkPoll) / 1000));
  }
  char buf[96];
  if (lastCallTg[0]) {
    snprintf(buf, sizeof(buf), "CALLS %d/min   %s %s", callsMin, lastCallTg, age);
  } else {
    snprintf(buf, sizeof(buf), "CALLS %d/min", callsMin);
  }
  g.setTextDatum(BC_DATUM);
  g.setTextColor(C_MUTED, C_BG);
  g.drawString(buf, SCREEN_W / 2, SCREEN_H - 2, 2);
}

void drawMain(TFT_eSprite &g) {
  g.fillSprite(C_BG);
  drawTopBar(g);

  if (viewMode == 0) {
    // split view: rdio left, thinline right
    g.drawFastVLine(SCREEN_W / 2, 28, SCREEN_H - 62, C_GREENDK);
    drawCount(g, rdioCount, SCREEN_W / 4, 78, false);
    drawCount(g, thinlineCount, 3 * SCREEN_W / 4, 78, false);
    g.setTextDatum(BC_DATUM);
    g.setTextColor(C_MUTED, C_BG);
    g.drawString("RDIO", SCREEN_W / 4, SCREEN_H - 26, 2);
    g.drawString("THINLINE", 3 * SCREEN_W / 4, SCREEN_H - 26, 2);
  } else if (viewMode == 3) {
    // calls zoom: rate huge, last talkgroup beneath
    drawCount(g, callsMin, SCREEN_W / 2, 74, true);
    g.setTextDatum(BC_DATUM);
    g.setTextColor(C_MUTED, C_BG);
    g.drawString("CALLS / MIN", SCREEN_W / 2, SCREEN_H - 40, 2);
    if (lastCallTg[0]) {
      g.setTextColor(C_GREEN, C_BG);
      g.drawString(lastCallTg, SCREEN_W / 2, SCREEN_H - 20, 2);
    }
  } else {
    int v = (viewMode == 1) ? rdioCount : thinlineCount;
    const char *label = (viewMode == 1) ? "RDIO LISTENERS" : "THINLINE LISTENERS";
    drawCount(g, v, SCREEN_W / 2, 78, true);
    g.setTextDatum(BC_DATUM);
    g.setTextColor(C_MUTED, C_BG);
    g.drawString(label, SCREEN_W / 2, SCREEN_H - 26, 2);
  }

  if (lastOkPoll == 0 || millis() - lastOkPoll > STALE_MS) {
    drawStaleBar(g);
  } else if (viewMode != 3) {
    drawCallStrip(g);
  }
  g.pushSprite(0, 0);
}

void drawMessage(const char *line1, const char *line2, uint16_t color) {
  spr.fillSprite(C_BG);
  spr.setTextDatum(MC_DATUM);
  spr.setTextColor(color, C_BG);
  spr.drawString(line1, SCREEN_W / 2, 70, 4);
  if (line2 && line2[0]) {
    spr.setTextColor(C_MUTED, C_BG);
    spr.drawString(line2, SCREEN_W / 2, 110, 2);
  }
  spr.pushSprite(0, 0);
}

// ---------------------------------------------------------------- network

void connectWiFi() {
  drawMessage("CONNECTING", WIFI_SSID, C_GREEN);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi connected: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("WiFi connect timeout");
    drawMessage("WIFI FAILED", "retrying...", C_RED);
  }
}

bool pollTicker() {
  HTTPClient http;
  http.setConnectTimeout(4000);
  http.setTimeout(4000);
  http.setReuse(true);
  if (!http.begin(TICKER_URL)) return false;
  http.addHeader("X-API-Key", API_KEY);
  int code = http.GET();
  bool ok = false;
  if (code == HTTP_CODE_OK) {
    JsonDocument filter;
    filter["rdio"] = true;
    filter["thinline"] = true;
    filter["status"] = true;
    filter["calls_min"] = true;
    filter["last_call"] = true;
    JsonDocument doc;
    DeserializationError err = deserializeJson(
        doc, http.getStream(), DeserializationOption::Filter(filter));
    if (!err) {
      rdioCount = doc["rdio"].isNull() ? -1 : doc["rdio"].as<int>();
      thinlineCount = doc["thinline"].isNull() ? -1 : doc["thinline"].as<int>();
      callsMin = doc["calls_min"].isNull() ? -1 : doc["calls_min"].as<int>();
      if (doc["last_call"].is<JsonObject>()) {
        strlcpy(lastCallTg, doc["last_call"]["talkgroup"] | "", sizeof(lastCallTg));
        lastCallAge = doc["last_call"]["age_s"] | -1;
      } else {
        lastCallTg[0] = 0;
        lastCallAge = -1;
      }
      strlcpy(sysStatus, doc["status"] | "unk", sizeof(sysStatus));
      lastOkPoll = millis();
      ok = true;
      Serial.printf("ticker: rdio=%d thinline=%d calls=%d status=%s\n",
                    rdioCount, thinlineCount, callsMin, sysStatus);
    } else {
      Serial.printf("json error: %s\n", err.c_str());
    }
  } else {
    Serial.printf("http error: %d\n", code);
  }
  http.end();
  return ok;
}

// ---------------------------------------------------------------- buttons

bool pressed(uint8_t pin) {
  static unsigned long lastPress[64] = {0};
  if (digitalRead(pin) == LOW && millis() - lastPress[pin] > 300) {
    lastPress[pin] = millis();
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- arduino

void setup() {
  // LCD power rail first — without this the screen is dark on battery
  pinMode(PIN_POWER_ON, OUTPUT);
  digitalWrite(PIN_POWER_ON, HIGH);

  Serial.begin(115200);
  pinMode(PIN_BTN_BRIGHT, INPUT_PULLUP);
  pinMode(PIN_BTN_VIEW, INPUT_PULLUP);

  tft.init();
  tft.setRotation(3);            // 320x170 landscape, USB on the left
  tft.fillScreen(C_BG);

  ledcSetup(0, 5000, 8);
  ledcAttachPin(PIN_LCD_BL, 0);
  setBrightness(BRIGHTNESS);

  spr.setColorDepth(16);
  useSprite = spr.createSprite(SCREEN_W, SCREEN_H) != nullptr;
  if (!useSprite) {
    // Shouldn't happen on the S3 (PSRAM), but fail loudly if it does
    tft.setTextColor(C_RED);
    tft.drawString("SPRITE ALLOC FAILED", 10, 10, 2);
  }

  drawMessage("HGA MONITOR", "listener counter", C_GREEN);
  delay(800);
  connectWiFi();
  lastPollAttempt = 0;           // poll immediately in loop()
}

void loop() {
  if (pressed(PIN_BTN_BRIGHT)) {
    brightIdx = (brightIdx + 1) % 4;
    setBrightness(brightLevels[brightIdx]);
  }
  if (pressed(PIN_BTN_VIEW)) {
    viewMode = (viewMode + 1) % 4;
    drawMain(spr);
  }

  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastRetry = 0;
    if (millis() - lastRetry > 10000) {
      lastRetry = millis();
      WiFi.disconnect();
      connectWiFi();
    }
  } else if (lastPollAttempt == 0 ||
             millis() - lastPollAttempt > POLL_SECONDS * 1000UL) {
    lastPollAttempt = millis();
    pollTicker();
    drawMain(spr);
  }

  // refresh once a second so the stale counter / wifi bars stay honest
  static unsigned long lastTick = 0;
  if (millis() - lastTick > 1000) {
    lastTick = millis();
    drawMain(spr);
  }
  delay(10);
}
