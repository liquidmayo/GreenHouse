# HGA Listener Counter — T-Display S3 AMOLED

AMOLED (1.91" RM67162, 536x240) version of the HGA desk display. Polls the
HGA Monitor master's `/api/ticker` over WiFi.

**Views** (KEY/GPIO21 tap cycles; hold ~1s toggles 8s auto-cycle):
1. Dashboard grid — rdio, ThinLine, YouTube viewers, calls/min at once
2. Listener trend — current count + 6-hour graph
3-7. Zoom views: rdio / thinline / calls / FB followers / viewers

BOOT button cycles day brightness. Night hours auto-dim (config.h).

**Burn-in care** (24/7 AMOLED): frame orbits ±3 px every minute, night
dimming via NTP local time, no pure-white pixels, black background = off
pixels.

## Build & flash

1. `cp include/config.h.example include/config.h` and fill in WiFi, the
   master's ticker URL, and the API key. Set night hours / TZ if needed.
2. Plug the board in (native USB, no drivers) and from this folder:

```
pio run -t upload
```

First flash of a factory-fresh board may need download mode: hold BOOT,
tap RST, release BOOT, then upload (press RST after to boot).

## Notes

- No PlatformIO board id exists upstream; `boards/T-Display-AMOLED.json`
  is vendored (16MB QIO flash + 8MB OPI PSRAM — different from the LCD).
- `src/rm67162.*` is LilyGo's factory QSPI driver, vendored, plus a
  `lcd_brightness()` addition. TFT_eSPI never touches hardware here — it
  renders the sprite that `lcd_PushColors()` pushes.
- USB-CDC-on-boot is off so the display never waits for a USB host on
  wall power; serial prints are not visible over USB.
- Touch-variant note: if a *touch* 1.91" board shows a black screen,
  GPIO38 is its panel power pin — drive HIGH before `rm67162_init()`.
  (On the non-touch board GPIO38 is just an LED, and GPIO21 is the KEY
  button rather than the touch IRQ.)
