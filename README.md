# 121GW for Linux

Native GTK4 / libadwaita companion app for the **EEVblog 121GW** multimeter, talking to the meter over
Bluetooth LE through BlueZ.

It is a port of the official [EEVblog app-121gw](https://gitlab.com/eevblog/app-121gw) and the
[evotronix](https://github.com/evotronix) iOS / Windows / Android / Chrome ports (iOS v1.4.22 feature set):
same packet decoder, key codes, logging behaviour and Live / Sessions / Connect layout.

Pure Python + PyGObject. BLE goes straight to BlueZ over D-Bus, so there is **nothing to `pip install`**.

## Features

**Card** and **Meter** (zombu4's open firmware `F-1.05` or later, custom `C-1.06` or later)

Put the meter on its SETUP page `Ld-bt` (SETUP, then REL / PEAK to the page after `bt`; logging off). Then:
- **Card**: lists the files on the meter's SD card with size and date, and the free space. Download a file, delete
  one (after a confirmation; `cal.bin`, `EEVBlog.bin` and `BTLOAD.TMP` can't be deleted, the meter needs them), or
  **Sync to Folder**: copies new and changed files into a local folder (default `~/Documents/121GW card`, the
  **Change…** button picks another). The sync never deletes anything, on either side. Every piece of a file is
  checked against the CRC-32 the meter computes, and a file is written as `<name>.part` and renamed only when it
  is complete.
- **Meter**: the meter's SETUP settings (buzzer, continuity threshold and hold, auto power-off, backlight auto-off,
  battery trim, LCD contrast, display level, REL view, clock, temperature unit, thermocouple type, dBm reference,
  current-loop and RTD views, VA burden, Bluetooth rate, logging trigger and change threshold). A change goes to the
  meter at once; the meter applies and stores it exactly as its own SETUP page does, so the app never writes the
  EEPROM itself. Calibration, identity and the logging interval are not offered by the firmware.
- With older firmware the pages say which version is needed.

**Bootloader…** (main menu): installs or updates zombu4's two-part bootloader from his newest `bl-v*` release. The
zip is checked against its published SHA-256 and every file against the `SHA256SUMS` inside. Then, depending on what
the meter reports:
- **Update** (the meter has his bootloader, e.g. `B-1.11`): the meter restarts into its update mode and receives
  `blimage.bin` (about 20 s) into a spare slot. After a restart its starter copies the new bootloader in. The
  running bootloader is replaced only after the whole image has arrived and been checked. Done on a real meter
  (B-1.11 → B-1.12).
- **First install** (the meter has EEVblog's bootloader, firmware `F-1.04` / `C-1.04` or later): with the meter on
  `Ld-bt`, the meter receives `install.bin` and checks the battery (refuses below 4.80 V, `LdE 8`) and that its
  bootloader is EEVblog's (`LdE 7` if not). It then writes the bootloader and restarts. For about 10 ms of that
  write a power cut would leave the meter unable to start, so keep fresh batteries in. Only checked in the
  emulator up to the meter's refusals: EEVblog's bootloader image isn't available here.
- Either way the app waits until the meter reports the new version. Calibration and settings are never written.

**Theme**: *Companion Dark* (the default) follows the look of zombu4's 121GW Companion: near-black, warm grey
text and the meter's yellow as the only accent. *System* (main menu → Theme) uses the desktop's own colours.

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
- **SETUP pages explained** (custom firmware `C-1.03` and later): while you step through the meter's SETUP pages,
  the app shows each page's name, its present setting in words (for example *Bluetooth rate: 20*, *4–20 mA loop view:
  4–20 mA as %*, *Low limit: 9.0000 V*) and what the page does, plus the meter's own text. It also marks the page while
  you're changing it. With older firmware most pages arrive as zeros, so the app can show only the pages EEVblog's
  v2.05 described.
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

  **Speed.** Many 121GWs answer the Bluetooth MTU exchange with 23 bytes (seen with `btmon`: `Server RX MTU: 23`), so
  one write carries 20 bytes. The app sends each packet in writes of that size (or of the link's MTU when larger) and
  sends a packet again after 3 s without an answer. With Linux's default connection interval (30–50 ms) a full image
  takes about 13 minutes (estimated from the measured 1.15 s per packet); with 15 ms for the meter about 5½ minutes (measured, C-1.03, 156 KB). To set 15 ms for the
  meter only, add to `/var/lib/bluetooth/<adapter>/<meter address>/info` (as root) and restart Bluetooth:

      [ConnectionParameters]
      MinInterval=12
      MaxInterval=12
      Latency=0
      Timeout=42

  Measured full uploads of the 156 KB image on one meter: 15 ms 5½ min (11 packets sent again); 7.5 ms (`6`) 17 min
  (282 sent again); 11.25 ms (`9`) 13½ min, the same speed as the default, so it was apparently not applied. The
  meter's serial line (19200 baud) limits any setting to about 1½ minutes. Every upload, slow or not, ended with the
  meter's own check (length, CRC-32, image header, copy read back): a lost packet costs time, not data.

  **Damaged answers (fixed in the app).** At short intervals the meter's Bluetooth module damages some of the meter's
  answers on its serial input: a one-bit slip in one of the first bytes (`5B` -> `2B`, `81` -> `60`, the packet number
  `0x20` -> `0x10`), while the meter had taken the packet. A capture at 7.5 ms showed about 1 answer in 6 damaged and
  no packet to the meter damaged at all; each damaged answer used to cost a 3 s wait. The app now repairs such an
  answer when its CRC-16 matches with the header and the awaited packet number put back (`repair_answer`). Measured
  2026-11-05 at 7.5 ms (`MinInterval=6`, `MaxInterval=6`): the 170 KB C-1.04 image in 3 minutes (982 B/s), 74 answers
  repaired, 2 packets sent again. That is zombu4's speed (155 kB in 160–170 s).

  On 2026-11-05 a capture showed 45 ms again on a later connection although the file still held the setting, so
  BlueZ may not apply it every time. `MinConnectionInterval=12` / `MaxConnectionInterval=12` in
  `/etc/bluetooth/main.conf` sets 15 ms for every LE device that doesn't ask for its own timing.

  **Direct install (zombu4's two-part bootloader, bl-v1.1.0 or later).** On a meter with firmware v1.0.4 or later
  (`C-1.04` included) the app asks which bootloader the meter has. With zombu4's bootloader the dialog offers
  **Install directly** (on by default): no SD card and no keys. The app restarts the meter into its update mode
  (`-IAP-`), connects again, and sends the file straight to the flash with two packets in flight. The bootloader
  checks the length and CRC-32 before it marks the firmware valid, then restarts into it. If the transfer stops halfway,
  the meter stays in its update mode until a firmware arrives; the app shows this in a banner, and the card route
  (MEM + HOLD) still works. In the emulator, against zombu4's bootloader code, a 170 KB image took 97 s, including
  7 packets dropped on purpose and sent again. With EEVblog's bootloader (`B-EEV`) the dialog keeps the card route.
- **Check for Firmware Update** (main menu): looks for the newest custom build (`custom-cA.BC` release with
  `EEVBlog.bin` and `SHA256SUMS`) on [bigtimer-dev/121GW-Open-Firmware](https://github.com/bigtimer-dev/121GW-Open-Firmware/releases).
  A meter running an older custom build is offered it automatically; the download is used only when its SHA-256, the
  image check and the version inside it match. While the meter is on SETUP page `Ld` (no readings) the app says so in
  a banner instead of "communication lost".
- **Statistics** (Live page, under the display): min, max, average, standard deviation (sample, n − 1), peak-to-peak
  and count of the readings, as zombu4's companion shows them, with the start time and a **Reset** button. Overload
  readings are skipped and counted. The values are in one unit per mode (V for DCV in every range), so autoranging
  doesn't restart them; a change of mode, AC/DC or unit does (`gw121/livestats.py`).
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
| Card files (Download, Sync) | `~/Documents/121GW card` (changeable); `.121gw-sync.json` there records what was copied |

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
- Card and settings (page `Ld`, open firmware `F-1.05` / `C-1.06`+, `gw121/card.py`): requests in the upload's
  packet framing, types `0x10` LIST, `0x11` READ (up to 8 KB staged in the meter, answered with its CRC-32), `0x12`
  CHUNK (232 bytes of the staged data), `0x13` DELETE, `0x14` INFO, `0x16` GET and `0x17` PUT of a setting (its
  EEPROM offset and value). Answers `B5 5B 82 seq(2) len status field(2) data crc16(2)`, one request at a time;
  answers with damaged first bytes are repaired as for the upload, when their CRC-16 proves it.
- Version request (open firmware v1.0.1+): `F4 39 30 39 30`; the meter echoes it and answers `F6` + five ASCII
  characters (`C-102`, shown as `C-1.02`) and an XOR byte that makes the XOR of all seven bytes zero
  (`gw121/keys.py` `parse_version_answer`).
- SETUP page text (custom firmware `C-1.03`+, `CF-07`): while a SETUP page is shown the meter also sends
  `FA item c0..c4 flags units 00 xor` (`F7` in `C-1.03`) (11 bytes): the page number, the five characters of the small display, the
  decimal point (flags bits 0..2), minus (bit 4), editing (bit 5), unit icons (V, A, m, k, Hz, dB, Ω) and an XOR byte
  that makes the XOR of all eleven bytes zero; item 0 once when the page is left (`gw121/setup_pages.py`).
- Firmware upload (open firmware v1.0.0+, page `Ld`, firmware `ble/bft.s`): packets `B5 5B type seq(2) len payload
  crc16(2)` (type 1 START with size and CRC-32, 2 DATA, 3 END, 4 ABORT; CRC-16/CCITT-FALSE), each answered
  `B5 5B 81 seq(2) 03 status next(2) crc16(2)` (`gw121/upload.py`). A STATUS packet (type 5) with the index `0x80`
  tells the two silent states apart: the page `Ld` refuses it (status 3), the bootloader's update mode answers status
  0 with its version (`major << 8 | minor << 4 | patch`).
- Bootloader (firmware v1.0.4+, `ble/blcmd.s`): key code `0x92` (`F4 39 32 39 32`) is answered with
  `F7 "B-EEV"` (EEVblog's bootloader) or `F7 "B-" + three digits` and an XOR byte; `0x91` (`F4 39 31 39 31`) restarts
  into zombu4's bootloader's update mode. There START also carries the kind (`0` = firmware), the meter erases before
  it answers, up to two packets may be in flight, and it restarts into the firmware after END (`BootSession`).
  The custom firmware's SETUP text frame starts with `FA` from `C-1.04` on (`F7` in `C-1.03`), since v1.0.4 uses
  `F7` for the bootloader answer.

## Development

```sh
python3 -m unittest discover -s tests
```

Layout: `packet.py` decoder · `bluez.py` BLE transport · `demo.py` simulator · `link.py` shared live state ·
`sessions.py` logging/CSV · `continuity.py` beeper · `chart.py` trend chart · `lcd.py` LCD widget ·
`window.py` UI · `app.py` entry point.

## License

MIT, like the upstream projects. See [LICENSE](LICENSE).
