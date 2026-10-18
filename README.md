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
  `Shift`+key = long press, `↑/↓` in SETUP, `Ctrl+L` log, `Ctrl+1/2/3` tabs

Vibration feedback from the mobile apps has no PC equivalent and is omitted. The original Xamarin app’s
multi-meter tabs aren’t carried over either; like all the evotronix ports, this app connects to one meter.

## Requirements

- Python ≥ 3.10, PyGObject, GTK ≥ 4.12, libadwaita ≥ 1.6, pycairo
- BlueZ (`bluetooth.service` running) and a Bluetooth LE adapter
- For sound: `pw-play`, `paplay` or `aplay`

Arch: `sudo pacman -S python-gobject python-cairo gtk4 libadwaita bluez bluez-utils`
Debian/Ubuntu (24.10+): `sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 bluez`
Fedora: `sudo dnf install python3-gobject python3-cairo gtk4 libadwaita bluez`

## Run

```sh
python3 -m gw121          # real meter
python3 -m gw121 --demo   # simulated meter
```

Install for your user (launcher in `~/.local/bin`, desktop entry and icon):

```sh
./install.sh              # then run `121gw` or open "121GW Multimeter" from the launcher
./install.sh --uninstall
```

## Connecting the meter

1. Make sure Bluetooth is on: `rfkill unblock bluetooth && bluetoothctl power on`
   (or use **Turn On Bluetooth** on the Connect tab).
2. On the 121GW hold **1ms PEAK** until the **BT** icon appears.
3. The app connects automatically (auto-reconnect), or press **Scan** on the Connect tab and pick the meter.
   Pairing is not needed.

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

## Development

```sh
python3 -m unittest discover -s tests
```

Layout: `packet.py` decoder · `bluez.py` BLE transport · `demo.py` simulator · `link.py` shared live state ·
`sessions.py` logging/CSV · `continuity.py` beeper · `chart.py` trend chart · `lcd.py` LCD widget ·
`window.py` UI · `app.py` entry point.

## License

MIT, like the upstream projects. See [LICENSE](LICENSE).
