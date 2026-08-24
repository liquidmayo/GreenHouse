// HGA Listener Counter — LilyGo T-Display S3 AMOLED (1.91" non-touch, RM67162)
//
// Polls the HGA Monitor master's /api/ticker over WiFi and shows live
// rdio-scanner + ThinLine listeners, YouTube viewers, call activity, FB
// followers, and a 6-hour listener trend graph — HomeGrown Alerts style.
//
// Rendering: TFT_eSPI is used purely as a sprite/font engine; frames are
// pushed to the panel through the vendored RM67162 QSPI driver
// (lcd_PushColors). setSwapBytes(true) is required on this path.
//
// AMOLED burn-in care (this panel runs 24/7):
//   - whole frame orbits +/-3 px on a slow cycle (viewport offset)
//   - scheduled night dimming via NTP local time
//   - no pure-white pixels; black background = pixels off
//
// Buttons: BOOT (GPIO0) = brightness levels (day levels; night auto-dims)
//          KEY (GPIO21) tap = next view, hold ~1s = auto-cycle (8s/view)

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <TFT_eSPI.h>
#include <time.h>
#include "config.h"
#include "rm67162.h"

#define PANEL_W 536
#define PANEL_H 240
// content is drawn inside a viewport that orbits within the 6px margin
#define ORBIT   6
#define CW (PANEL_W - ORBIT)
#define CH (PANEL_H - ORBIT)
#define ORBIT_STEP_MS 60000UL

#define PIN_BTN_BRIGHT 0    // BOOT button
#define PIN_BTN_VIEW   21   // KEY button (non-touch board; touch IRQ on touch variant)

// ---- HGA palette (RGB565) ----
#define C_BG      0x0000
#define C_GREEN   0x262B    // #22c55e
#define C_GREENDK 0x1326    // #166534
#define C_MUTED   0xA5F5    // #a6beac
#define C_TEXT    0xC618    // dim white — never pure white on OLED
#define C_YELLOW  0xED81    // #eab308
#define C_RED     0xEA28    // #ef4444
#define C_GRAY    0x39E7

#define STALE_MS 60000UL
#define VIEW_COUNT 7        // 0 grid, 1 trend, 2 rdio, 3 thinline, 4 calls, 5 followers, 6 viewers
#define AUTO_CYCLE_MS 8000UL
#define LONG_PRESS_MS 900UL

TFT_eSPI tft;               // never init'd — sprite/font engine only
TFT_eSprite spr(&tft);

// live values (-1 = unknown)
int rdioCount = -1, thinlineCount = -1, callsMin = -1;
int followersTotal = -1, viewersCount = -1;
float sparkVals[24];
int sparkLen = 0;
char lastCallTg[48] = "";
int lastCallAge = -1;
char sysStatus[8] = "unk";
unsigned long lastOkPoll = 0, lastPollAttempt = 0;
int viewMode = 0;
bool autoCycle = false;
unsigned long lastAutoAdvance = 0;

// orbit state: walks the square perimeter of the 0..ORBIT margin
int orbX = ORBIT / 2, orbY = ORBIT / 2;
unsigned long lastOrbit = 0;
int orbPhase = 0;

const uint8_t dayLevels[] = {60, 120, BRIGHT_DAY, 255};
int brightIdx = 2;
bool nightNow = false;

// ---------------------------------------------------------------- helpers

uint16_t statusColor() {
  if (strcmp(sysStatus, "crit") == 0) return C_RED;
  if (strcmp(sysStatus, "warn") == 0) return C_YELLOW;
  if (strcmp(sysStatus, "ok") == 0) return C_GREEN;
  return C_GRAY;
}

void fmtAgeShort(char *out, size_t n, int seconds) {
  if (seconds < 0) { out[0] = 0; return; }
  if (seconds < 120) snprintf(out, n, "%ds", seconds);
  else if (seconds < 5400) snprintf(out, n, "%dm", seconds / 60);
  else snprintf(out, n, "%dh", seconds / 3600);
}

bool timeValid(struct tm &t) {
  return getLocalTime(&t, 0) && t.tm_year > 120;
}

bool isNight() {
  struct tm t;
  if (!timeValid(t)) return false;
  int h = t.tm_hour;
  if (NIGHT_START <= NIGHT_END) return h >= NIGHT_START && h < NIGHT_END;
  return h >= NIGHT_START || h < NIGHT_END;   // window crosses midnight
}

void applyBrightness() {
  nightNow = isNight();
  lcd_brightness(nightNow ? BRIGHT_NIGHT : dayLevels[brightIdx]);
}

void pushFrame() {
  lcd_PushColors(0, 0, PANEL_W, PANEL_H, (uint16_t *)spr.getPointer());
}

// ---------------------------------------------------------------- drawing

// draw a big number centered at (cx, cy). zoom=true uses the largest fonts.
void drawCount(TFT_eSprite &g, int value, int cx, int cy, bool zoom) {
  g.setTextDatum(MC_DATUM);
  if (value < 0) {
    g.setTextColor(C_GRAY, C_BG);
    g.drawString("--", cx, cy, 4);
    return;
  }
  g.setTextColor(C_GREEN, C_BG);
  int digits = 1;
  for (int v = value; v >= 10; v /= 10) digits++;
  if (zoom) {
    if (digits <= 3) { g.setTextSize(2); g.drawNumber(value, cx, cy, 8); g.setTextSize(1); }
    else if (digits <= 6) g.drawNumber(value, cx, cy, 8);
    else g.drawNumber(value, cx, cy, 7);
  } else {
    g.drawNumber(value, cx, cy, digits <= 7 ? 7 : 6);
  }
}

void drawTopBar(TFT_eSprite &g) {
  if (WiFi.status() == WL_CONNECTED) {
    int rssi = WiFi.RSSI();
    int bars = (rssi > -55) ? 4 : (rssi > -65) ? 3 : (rssi > -75) ? 2 : 1;
    for (int i = 0; i < 4; i++) {
      int h = 3 + i * 3;
      g.fillRect(8 + i * 6, 18 - h, 4, h, (i < bars) ? C_GREEN : C_GREENDK);
    }
  } else {
    g.setTextDatum(TL_DATUM);
    g.setTextColor(C_RED, C_BG);
    g.drawString("NO WIFI", 8, 5, 2);
  }
  g.setTextDatum(TC_DATUM);
  g.setTextColor(C_MUTED, C_BG);
  g.drawString("HOMEGROWN ALERTS", CW / 2, 5, 2);
  // local clock (NTP), left of the status dot
  struct tm t;
  if (timeValid(t)) {
    char buf[8];
    snprintf(buf, sizeof(buf), "%02d:%02d", t.tm_hour, t.tm_min);
    g.setTextDatum(TR_DATUM);
    g.setTextColor(C_GRAY, C_BG);
    g.drawString(buf, CW - 44, 5, 2);
  }
  g.fillSmoothCircle(CW - 14, 11, 6, statusColor(), C_BG);
  if (autoCycle) g.drawSmoothCircle(CW - 30, 11, 5, C_GREEN, C_BG);
}

void drawStaleBar(TFT_eSprite &g) {
  unsigned long age = (millis() - lastOkPoll) / 1000;
  g.setTextDatum(BC_DATUM);
  g.setTextColor(C_YELLOW, C_BG);
  char buf[32];
  snprintf(buf, sizeof(buf), "STALE %lus", age);
  g.drawString(buf, CW / 2, CH - 2, 2);
}

void drawCallStrip(TFT_eSprite &g) {
  if (callsMin < 0) return;
  char age[12] = "";
  if (lastCallAge >= 0 && lastOkPoll > 0) {
    fmtAgeShort(age, sizeof(age), lastCallAge + (int)((millis() - lastOkPoll) / 1000));
  }
  char buf[110];
  if (lastCallTg[0]) snprintf(buf, sizeof(buf), "CALLS %d/min   %s %s", callsMin, lastCallTg, age);
  else snprintf(buf, sizeof(buf), "CALLS %d/min", callsMin);
  g.setTextDatum(BC_DATUM);
  g.setTextColor(C_MUTED, C_BG);
  g.drawString(buf, CW / 2, CH - 2, 2);
}

void gridCell(TFT_eSprite &g, int cx, int cy, int value, const char *label) {
  drawCount(g, value, cx, cy, false);
  g.setTextDatum(TC_DATUM);
  g.setTextColor(C_MUTED, C_BG);
  g.drawString(label, cx, cy + 30, 2);
}

void drawDashboard(TFT_eSprite &g) {
  // 2x2 grid of the four live numbers
  int colL = CW / 4, colR = 3 * CW / 4;
  int rowT = 70, rowB = 156;
  g.drawFastVLine(CW / 2, 32, CH - 62, C_GREENDK);
  g.drawFastHLine(24, (rowT + rowB) / 2 + 8, CW - 48, C_GREENDK);
  gridCell(g, colL, rowT, rdioCount, "RDIO LISTENERS");
  gridCell(g, colR, rowT, thinlineCount, "THINLINE LISTENERS");
  gridCell(g, colL, rowB, viewersCount, "YOUTUBE VIEWERS");
  gridCell(g, colR, rowB, callsMin, "CALLS / MIN");
}

void drawTrend(TFT_eSprite &g) {
  // left: current merged rdio listeners; right: 6h sparkline
  drawCount(g, rdioCount, 92, 104, false);
  g.setTextDatum(TC_DATUM);
  g.setTextColor(C_MUTED, C_BG);
  g.drawString("RDIO LISTENERS", 92, 140, 2);
  g.drawString("LAST 6 HOURS", (190 + CW - 16) / 2, CH - 34, 2);

  int gx = 190, gy = 34, gw = CW - 16 - gx, gh = CH - 78 - gy;
  g.drawRect(gx - 1, gy - 1, gw + 2, gh + 2, C_GREENDK);
  if (sparkLen < 2) {
    g.setTextDatum(MC_DATUM);
    g.setTextColor(C_GRAY, C_BG);
    g.drawString("collecting history...", gx + gw / 2, gy + gh / 2, 2);
    return;
  }
  float mn = sparkVals[0], mx = sparkVals[0];
  for (int i = 1; i < sparkLen; i++) {
    if (sparkVals[i] < mn) mn = sparkVals[i];
    if (sparkVals[i] > mx) mx = sparkVals[i];
  }
  if (mx - mn < 1) mx = mn + 1;
  int px = -1, py = -1;
  for (int i = 0; i < sparkLen; i++) {
    int x = gx + (int)((float)i / (sparkLen - 1) * (gw - 1));
    int y = gy + gh - 1 - (int)((sparkVals[i] - mn) / (mx - mn) * (gh - 2));
    if (px >= 0) {
      g.drawLine(px, py, x, y, C_GREEN);
      g.drawLine(px, py + 1, x, y + 1, C_GREENDK);   // soft glow underline
    }
    px = x; py = y;
  }
  g.setTextDatum(TL_DATUM);
  g.setTextColor(C_GRAY, C_BG);
  g.drawNumber((int)mx, gx + 3, gy + 2, 2);
  g.setTextDatum(BL_DATUM);
  g.drawNumber((int)mn, gx + 3, gy + gh - 2, 2);
}

void drawZoom(TFT_eSprite &g, int value, const char *label, const char *sub) {
  drawCount(g, value, CW / 2, 104, true);
  g.setTextDatum(BC_DATUM);
  g.setTextColor(C_MUTED, C_BG);
  g.drawString(label, CW / 2, CH - 34, 4);
  if (sub && sub[0]) {
    g.setTextColor(C_GREEN, C_BG);
    g.drawString(sub, CW / 2, CH - 14, 2);
  }
}

void drawMain(TFT_eSprite &g) {
  g.fillSprite(C_BG);
  g.setViewport(orbX, orbY, CW, CH);
  drawTopBar(g);
  switch (viewMode) {
    case 0: drawDashboard(g); break;
    case 1: drawTrend(g); break;
    case 2: drawZoom(g, rdioCount, "RDIO LISTENERS", nullptr); break;
    case 3: drawZoom(g, thinlineCount, "THINLINE LISTENERS", nullptr); break;
    case 4: drawZoom(g, callsMin, "CALLS / MIN", lastCallTg); break;
    case 5: drawZoom(g, followersTotal, "FB FOLLOWERS", nullptr); break;
    case 6: drawZoom(g, viewersCount, "YOUTUBE VIEWERS", nullptr); break;
  }
  if (lastOkPoll == 0 || millis() - lastOkPoll > STALE_MS) drawStaleBar(g);
  else if (viewMode != 0 && viewMode != 4) drawCallStrip(g);
  g.resetViewport();
  pushFrame();
}

void drawMessage(const char *line1, const char *line2, uint16_t color) {
  spr.fillSprite(C_BG);
  spr.setViewport(orbX, orbY, CW, CH);
  spr.setTextDatum(MC_DATUM);
  spr.setTextColor(color, C_BG);
  spr.drawString(line1, CW / 2, 96, 4);
  if (line2 && line2[0]) {
    spr.setTextColor(C_MUTED, C_BG);
    spr.drawString(line2, CW / 2, 140, 2);
  }
  spr.resetViewport();
  pushFrame();
}

void stepOrbit() {
  // walk the square perimeter 0..ORBIT in 1px steps, one step per interval
  static const int path[8][2] = {{0,0},{3,0},{6,0},{6,3},{6,6},{3,6},{0,6},{0,3}};
  orbPhase = (orbPhase + 1) % 8;
  orbX = path[orbPhase][0];
  orbY = path[orbPhase][1];
}

// ---------------------------------------------------------------- network

void connectWiFi() {
  drawMessage("CONNECTING", WIFI_SSID, C_GREEN);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) delay(250);
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi connected: %s\n", WiFi.localIP().toString().c_str());
    configTzTime(TZ_STRING, "pool.ntp.org", "time.nist.gov");
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
    filter["followers"] = true;
    filter["viewers"] = true;
    filter["spark"] = true;
    JsonDocument doc;
    DeserializationError err = deserializeJson(
        doc, http.getStream(), DeserializationOption::Filter(filter));
    if (!err) {
      rdioCount = doc["rdio"].isNull() ? -1 : doc["rdio"].as<int>();
      thinlineCount = doc["thinline"].isNull() ? -1 : doc["thinline"].as<int>();
      callsMin = doc["calls_min"].isNull() ? -1 : doc["calls_min"].as<int>();
      followersTotal = doc["followers"].isNull() ? -1 : doc["followers"].as<int>();
      viewersCount = doc["viewers"].isNull() ? -1 : doc["viewers"].as<int>();
      sparkLen = 0;
      if (doc["spark"].is<JsonArray>()) {
        for (JsonVariant v : doc["spark"].as<JsonArray>()) {
          if (sparkLen >= 24) break;
          sparkVals[sparkLen++] = v.as<float>();
        }
      }
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
      Serial.printf("ticker: rdio=%d thinline=%d calls=%d viewers=%d spark=%d\n",
                    rdioCount, thinlineCount, callsMin, viewersCount, sparkLen);
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

// 0 = nothing, 1 = short press (on release), 2 = long press (fires while held)
int viewButtonEvent() {
  static bool wasDown = false;
  static unsigned long downAt = 0;
  static bool longFired = false;
  bool down = digitalRead(PIN_BTN_VIEW) == LOW;
  unsigned long now = millis();
  if (down && !wasDown) { wasDown = true; downAt = now; longFired = false; return 0; }
  if (down && wasDown && !longFired && now - downAt >= LONG_PRESS_MS) { longFired = true; return 2; }
  if (!down && wasDown) {
    wasDown = false;
    if (!longFired && now - downAt > 30) return 1;
  }
  return 0;
}

// ---------------------------------------------------------------- arduino

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BTN_BRIGHT, INPUT_PULLUP);
  pinMode(PIN_BTN_VIEW, INPUT_PULLUP);

  rm67162_init();
  lcd_setRotation(3);              // 536x240 landscape, flipped for desk orientation

  spr.setColorDepth(16);
  if (spr.createSprite(PANEL_W, PANEL_H) == nullptr) {
    Serial.println("FATAL: sprite alloc failed (PSRAM?)");
  }
  spr.setSwapBytes(true);          // required on the QSPI push path

  lcd_brightness(dayLevels[brightIdx]);
  drawMessage("HGA MONITOR", "amoled listener counter", C_GREEN);
  delay(800);
  connectWiFi();
  applyBrightness();
  lastPollAttempt = 0;
  lastOrbit = millis();
}

void loop() {
  if (pressed(PIN_BTN_BRIGHT)) {
    brightIdx = (brightIdx + 1) % 4;
    applyBrightness();
  }
  int ev = viewButtonEvent();
  if (ev == 1) {
    viewMode = (viewMode + 1) % VIEW_COUNT;
    lastAutoAdvance = millis();
    drawMain(spr);
  } else if (ev == 2) {
    autoCycle = !autoCycle;
    lastAutoAdvance = millis();
    drawMessage(autoCycle ? "AUTO CYCLE ON" : "AUTO CYCLE OFF",
                autoCycle ? "views rotate every 8s" : "hold button to re-enable", C_GREEN);
    delay(700);
    drawMain(spr);
  }
  if (autoCycle && millis() - lastAutoAdvance >= AUTO_CYCLE_MS) {
    lastAutoAdvance = millis();
    viewMode = (viewMode + 1) % VIEW_COUNT;
    drawMain(spr);
  }

  if (millis() - lastOrbit >= ORBIT_STEP_MS) {
    lastOrbit = millis();
    stepOrbit();
    applyBrightness();             // re-evaluate night window once a minute
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

  static unsigned long lastTick = 0;
  if (millis() - lastTick > 1000) {
    lastTick = millis();
    drawMain(spr);
  }
  delay(10);
}
