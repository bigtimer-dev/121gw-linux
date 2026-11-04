# 121GW for Linux

Native GTK4 / libadwaita companion app for the **EEVblog 121GW** multimeter, talking to the meter over
Bluetooth LE through BlueZ.

It is a port of the official [EEVblog app-121gw](https://gitlab.com/eevblog/app-121gw) and the
[evotronix](https://github.com/evotronix) iOS / Windows / Android / Chrome ports (iOS v1.4.22 feature set):
same packet decoder, key codes, logging behaviour and Live / Sessions / Connect layout.

Pure Python + PyGObject. BLE goes straight to BlueZ over D-Bus, so there is **nothing to `pip install`**.

## Features

**Live**
- LCD replica: BT, AUTO, 1kHz, 1ms, LowZ, BAT, APO, DC / AC / DC+AC, DIODE, CONT, REL, HOLD,
  MIN / MAX / AVG, MEM, dBm, TEST annunciators, sign, overload (OL)
- 26-segment analog bargraph with 0–5 / 0–10 scale and range marker (from the original EEVblog app)
- Secondary display: internal temperature, frequency (AC V), dBm, burden voltage, beeper threshold
- SETUP pages decoded: battery, time, date, year, beeper, auto power-off, LCD contrast, logging interval,
  meter ID, calibration
- Header: link RSSI (dBm), meter battery voltage, meter clock
- Warnings: communication lost (5 s watchdog), battery low (≤ 4.5 V) and near power-off (≤ 4.2 V), also
  as desktop notifications when the window is in the background
- Remote keys HOLD / MODE / REL▲ / RANGE / MIN-MAX / MEM / PEAK▼ / SETUP with short and long press
  (MODE long ≈ backlight); REL / PEAK act as ▲ / ▼ in the SETUP menu
- **Sync Meter Clock** (button under the keys, main menu or `Ctrl+T`): sets the meter's clock to the computer's
  local time, so SD-card log files get the right date. The meter confirms by echoing the frame. Open firmware
  `F-1.01` / `C-1.02` and later take every clock frame, so you can sync again at any time. v2.05 and older open
  firmware take one clock update each time Bluetooth is switched on: to sync again, hold **PEAK** on the meter
  (Bluetooth off), hold it again (on) and sync.
- **Firmware version**: on connect the app asks the meter for its firmware version (open firmware v1.0.1 and later
  answer, e.g. `C-1.02`) and shows it next to the meter on the Connect row. v2.05 and older open firmware ignore the
  request; nothing changes on the meter.
- **Upload Firmware…** (main menu), for zombu4's open firmware v1.0.0 or later (`C-1.02` included): sends a firmware
  file (`EEVBlog.bin`) over Bluetooth to the meter's SD card, so updating no longer needs the card in a computer.
  1. On the meter, turn Bluetooth on (hold PEAK), turn logging off and put the SD card in.
  2. Go to SETUP page `Ld` (after `bt`); the meter shows `Ld-bt` and stops sending readings while that page is shown.
  3. In the app, choose the file and press Upload. The app checks the file first (size, a valid 121GW image, and
     the version it contains). The meter shows `Ld nn` (percent), then `Ld oK` when `EEVBlog.bin` is written and
     read back.
  4. To install it, use the usual key sequence: meter off, hold **MEM + HOLD** while switching on, press **SETUP**.

  The meter writes to a temporary file and replaces `EEVBlog.bin` only after the length and CRC-32 match, so a
  cancelled or broken upload leaves the card as it was. The upload takes a few minutes (stop and wait, one 232-byte
  packet per answer).
- **Meter Views** (main menu), for meters with zombu4's open firmware v0.9.6 or later: shows DC mA as percent of a
  **4–20 mA / 0–20 mA loop** and Ω as the temperature of a **PT100 / PT1000** sensor, as the meter's SETUP pages
  `LP` and `Pt` do. The Bluetooth frame doesn't say which view the meter shows, so set the same view here. The app
  replays the firmware's own arithmetic on the same counts, so it shows the same digits as the meter's LCD. The PT
  view uses the meter's temperature unit, learnt from its TEMP function. As on the meter, only the main reading
  changes: the chart, the logs and the CSV keep mA / Ω.
- Trend chart: Y axis follows the meter mode/range (or “Auto Y”), mouse-wheel zoom, drag to pan,
  double-click to reset, hover readout, dashed internal-temperature trace with show/hide toggle.
  It shows a rolling preview before logging starts and the full 25,000-sample window while logging.

**Logging & sessions**
- Start / Stop & Save, with a session name prompt
- Logging pauses when the measurement *function* changes (range changes stay in the same log and are
  normalized for the graph). You are asked whether to save, then logging resumes.
- Session list with statistics (samples, duration, min, max, average, peak-peak, std-dev, rate)
- Notes (auto-saved), rename, delete, delete all
- Export full-resolution **CSV** and the **graph as PNG**

**Connect**
- Scan, device list with RSSI, connect, disconnect
- Auto-reconnect to the last meter (scans in the background and connects as soon as it advertises)
- “Show all BLE devices” for meters that advertise without their name
- Detects a powered-off or rfkill-blocked adapter and offers “Turn On Bluetooth”
- Continuity feedback: beep with ON/OFF ohm hysteresis (defaults ON < 25 Ω, OFF > 400 Ω), diode-conduction
  beep, ON threshold synced from the meter’s beeper setting, test beep

**Linux extras**
- `--demo` simulated meter (real frames through the real parser, reacts to the keys) to try the app
  without hardware
- Keyboard shortcuts: `H` HOLD, `M` MODE, `R` REL, `G` RANGE, `X` MIN/MAX, `E` MEM, `P` PEAK, `S` SETUP,
  `Shift`+key = long press, `↑/↓` in SETUP, `Ctrl+L` log, `Ctrl+T` sync clock, `Ctrl+1/2/3` tabs

Vibration feedback from the mobile apps has no PC equivalent and is omitted. The original Xamarin app’s
multi-meter tabs aren’t carried over either; like all the evotronix ports, this app connects to one meter.

## Install

One command: it checks/installs the dependencies (Arch, Debian/Ubuntu, Fedora, openSUSE), downloads the
app and adds **121GW Multimeter** to your app launcher. No root needed except for missing system packages.

```sh
curl -fsSL https://raw.githubusercontent.com/bigtimer-dev/121gw-linux/main/install.sh | bash
```

Or from a clone:

```sh
git clone https://github.com/bigtimer-dev/121gw-linux.git
cd 121gw-linux
./install.sh
```

Then open **121GW Multimeter** from your launcher, or run `121gw` (`121gw --demo` without a meter).
Run the same command again to update. Uninstall with `./install.sh --uninstall`
(or `curl … | bash -s -- --uninstall`). Installer options: `--yes` (install missing packages without asking),
`--no-deps` (skip the dependency check).

### Requirements

- Python ≥ 3.10, PyGObject, pycairo, GTK ≥ 4.12, libadwaita ≥ 1.6
  (Arch, Fedora 41+, Ubuntu 25.04+, Debian 13+, openSUSE Tumbleweed)
- BlueZ (`bluetooth.service` running) and a Bluetooth LE adapter
- For the continuity beep: `pw-play`, `paplay` or `aplay`

### Run without installing

```sh
python3 -m gw121          # real meter
python3 -m gw121 --demo   # simulated meter
```

## Connecting the meter

1. Make sure Bluetooth is on: `rfkill unblock bluetooth && bluetoothctl power on`
   (or use **Turn On Bluetooth** on the Connect tab).
2. On the 121GW hold **1ms PEAK** until the **BT** icon appears.
3. The app connects automatically (auto-reconnect), or press **Scan** on the Connect tab and pick the meter.
   Pairing is not needed.

## Meter firmware

Works with EEVblog's original firmware **v2.05** and with
[zombu4's open firmware](https://github.com/zombu4/121GW-Open-Firmware) (fast builds `F-0.9x` / `F-1.0x`, and the bug-fix
builds `C-0.96` / `C-1.02` from [bigtimer-dev/121GW-Open-Firmware](https://github.com/bigtimer-dev/121GW-Open-Firmware)). The open
firmware keeps the Bluetooth frame format, the key codes and the clock frame unchanged. Checked against `C-0.94`,
`C-0.96` and `C-1.02` (zombu4's v1.0.2 plus fixes) running in zombu4's emulator, with the app's own decoder and key frames:

- every frame decoded at the default 2 readings/s and at the open firmware's fastest **20 readings/s** (SETUP page
  `bt`); the continuity beep is rate-limited, so the faster rate adds no beep storm
- the new SETUP pages (`bL`, `bt`, `rEL`, `dSP`, `CL`, `Lt`, `Lo`, `HI`, `tr`, the limit-test verdict) decode without
  errors; their sub-display is sent as zero, so the app shows the main reading only
- overload: the open firmware sends the real bar segment count (at most 25), where v2.05 sent out-of-range values
- HOLD, MIN/MAX and REL from the app work; while the open firmware's limit test (`Lt`) runs, the meter refuses them on
  purpose, exactly as for the buttons on the meter
- REL in percent and the dBm reference change only the meter's own display; the app shows the classic REL value
  and dBm
- v0.9.6's 4–20 mA loop view (`LP`) and PT100 / PT1000 view (`Pt`) also change only the meter's display, and the
  frame keeps mA / Ω. With **Meter Views** set the same way, the app shows the meter's digits (cross-checked against the
  emulated meter's LCD across both loop spans, PT100 and PT1000, °C and °F, the ends of the span and `OFL`)
- thermocouple types N, R and S (v0.9.6, SETUP page `tc`) arrive as normal temperatures; the chart scale extends
  to 1800 °C / 3300 °F when an R or S reading goes above the type K span
- v0.9.6's auto-hold at 0 Ω / 0 V (OHM, DIODE) only changes when v2.05's auto-hold captures a value; the app shows
  it as `A-HOLD`, as before
- v1.0.1's version request: `C-1.02` answers with its version, which the app shows; `C-0.96` only echoes the request,
  with mode, SETUP page, HOLD and REL unchanged
- the Bluetooth firmware upload (SETUP page `Ld`): the app's sender (`gw121/upload.py`) sent the whole `C-1.02` image,
  including one damaged and one lost packet, to an emulated `C-1.02`; the emulated card then held an identical
  `EEVBlog.bin`

## Files

| What | Where |
|---|---|
| Settings | `~/.config/121gw/settings.json` |
| Sessions | `~/.local/share/121gw/sessions.json` |

## Protocol notes

- BLE service `0bd51666-e7cb-469b-8e4d-2742f1ba77cc` (Bluegiga Cable Replacement); data characteristic
  `e7add780-b042-4876-aae1-112855353cc1`, frames arrive as **indications**
- 19-byte frames: `0xF2`, …, XOR checksum of bytes 0–17 in byte 18 (`gw121/packet.py`)
- Keys: `F4 k c k c`, `k = 0x30` short / `0x38` long, `c = 0x31…0x38` (RANGE, HOLD, REL, PEAK, MODE,
  MIN/MAX, MEM, SETUP)
- Clock: `F8` + year − 2000, month, day, hour, minute, second and their XOR, each as two upper-case ASCII hex
  digits (15 bytes, `gw121/keys.py` `clock_payload`). The meter echoes the frame. v2.05 applies the first valid one
  after power-on or after its Bluetooth is switched on; open firmware v1.0.1 and later apply every one (`C-1.02`
  refuses dates and times outside the calendar: no echo).
- Version request (open firmware v1.0.1+): `F4 39 30 39 30`; the meter echoes it and answers `F6` + five ASCII
  characters (`C-102`, shown as `C-1.02`) and an XOR byte that makes the XOR of all seven bytes zero
  (`gw121/keys.py` `parse_version_answer`).
- Firmware upload (open firmware v1.0.0+, page `Ld`, firmware `ble/bft.s`): packets `B5 5B type seq(2) len payload
  crc16(2)` (type 1 START with size and CRC-32, 2 DATA, 3 END, 4 ABORT; CRC-16/CCITT-FALSE), each answered
  `B5 5B 81 seq(2) 03 status next(2) crc16(2)` (`gw121/upload.py`).

## Development

```sh
python3 -m unittest discover -s tests
```

Layout: `packet.py` decoder · `bluez.py` BLE transport · `demo.py` simulator · `link.py` shared live state ·
`sessions.py` logging/CSV · `continuity.py` beeper · `chart.py` trend chart · `lcd.py` LCD widget ·
`window.py` UI · `app.py` entry point.

## License

MIT, like the upstream projects. See [LICENSE](LICENSE).
