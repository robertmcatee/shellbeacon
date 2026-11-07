# ShellBeacon — telemetry, weather, and RF fork

A small APRS beacon in pure shell script, for a Raspberry Pi or any
Linux box. Every 20 minutes it sends, for your station:

- a **position** packet with your station comment,
- a **telemetry** packet reporting the host itself — CPU temperature,
  load, free memory, disk use, uptime, and an optional web-server
  health bit — with the standard PARM/UNIT/EQNS/BITS definitions,
- a **weather** field carrying live outdoor temperature, when a
  companion collector is running (below), and
- optionally, **the same frames on RF**: 144.390 MHz AX.25 AFSK,
  generated with Dire Wolf's `gen_packets` and transmitted with
  **rpitx** from a GPIO pin — no radio required, and
- optionally, a **spoken voice announcement** on a 2 m FM simplex
  channel (ID, time, outdoor temperature, station health) through the
  same rpitx path.

It is a fork of **shellbeacon 1.0 by WA1GOV**, a minimal netcat
APRS-IS beacon. The fork (Robert McAtee, **KJ5GBY**) adds the
telemetry, weather, and RF layers, and moves all station settings —
including the APRS-IS passcode — out of the script into a private
config file.

The reference station, KJ5GBY in San Antonio, Texas, runs it on a
Raspberry Pi Zero W: position and telemetry to APRS-IS, patio
temperature decoded from a 433 MHz sensor by an RTL-SDR receiver, and
simultaneous RF transmission on 144.390 at ~10 mW into a quarter-wave
ground-plane antenna, and a voice announcement on 146.430 MHz. Its
weather and telemetry are public on aprs.fi.

## Files

| File | Purpose |
| --- | --- |
| `shellbeacon.sh` | the beacon (APRS-IS + telemetry + WX + optional RF + voice) |
| `shellbeacon.conf.example` | station configuration template — copy to `shellbeacon.conf`, edit, keep private |
| `wx-collect.sh` | companion collector: keeps the latest outdoor-sensor reading in `~/wx/latest.json` |
| `web/` | companion status page: weather, transmitted telemetry, and a locator chart as one static page — see `web/README.md` |

## Requirements

- bash, netcat (`nc -C` — OpenBSD netcat), curl, awk
- for telemetry on a Pi: `vcgencmd` (Raspberry Pi OS ships it)
- for weather: an RTL-SDR receiver, `rtl_433`, and a supported 433 MHz
  temperature sensor (the reference station uses a Better Homes &
  Gardens unit speaking the Acurite-606TX protocol)
- for RF: [Dire Wolf](https://github.com/wb2osz/direwolf) (for
  `gen_packets`) and [rpitx](https://github.com/F5OEO/rpitx)
  (including its bundled `csdr`)
- for voice: `espeak-ng`, and root (rpitx opens `/dev/mem`, so the
  beacon must run as root for the RF and voice paths)

## Quick start

```sh
cp shellbeacon.conf.example shellbeacon.conf
chmod 600 shellbeacon.conf
# edit shellbeacon.conf: callsign, passcode, position, comment
./shellbeacon.sh 1    # one cycle, then exit
./shellbeacon.sh      # beacon every 20 minutes
```

Run it under systemd, cron, or whatever you like. The telemetry
sequence number persists in `tele.seq` next to the script, so
restarts don't reset it (aprs.fi keys telemetry by sequence).

## Status page

`web/` holds a companion generator that renders the station's status
as a single static web page — current weather with a 24-hour trace,
the last telemetry frame as transmitted (decoded from the beacon's
journal), station status in the beacon's own vocabulary, and a drawn
Maidenhead locator chart with the station pinned — Solarized, dark by
default, no external assets. The reference station's page is live at
<https://www.cenzontle.us/kj5gby/>. See `web/README.md`.

## Weather

`wx-collect.sh` runs `rtl_433` against the receiver and keeps the
newest matching sensor frame in `~/wx/latest.json` (edit the model
filter in the script for your sensor — the reference sensor re-IDs at
every battery swap, so matching is by model only). Run it detached —
an `@reboot` crontab entry plus its built-in lock works well. The
beacon folds any reading fresher than 40 minutes into a complete APRS
weather block; stale readings are dropped, never republished.

## RF transmission

Transmission is deliberately opt-in: it happens only while the flag
file `rf.enabled` exists next to the script.

```sh
touch rf.enabled     # arm RF (144.390 MHz, GPIO 4 / header pin 7)
rm rf.enabled        # back to internet-only
```

On RF the internet-only `TCPIP` path marker is replaced with
`WIDE1-1,WIDE2-1`. A ~19 in (quarter-wave) wire on the GPIO pin is the
antenna at 2 m. **Note:** raw rpitx output is rich in harmonics — a
low-pass filter in the antenna line is the correct permanent
arrangement, and as control operator you are responsible for your
station's emissions.

**Implementation notes (learned the hard way, rev 1.5–1.6):** the
rpitx 2.0 binary has no `-m FM` mode — its modes are IQ/IQFLOAT/RF/
RFA/VFO — and its `-f` flag is in **kHz**, not Hz. Narrow-band FM is
done the way the suite's own `testnfm.sh` does it: 48 kHz mono audio
piped through csdr into RF-pair mode —

```sh
csdr convert_i16_f | csdr gain_ff 7000 | csdr convert_f_samplerf 20833 \
  | rpitx -i- -m RF -f 146430        # note: kHz
```

Anything else runs cleanly, logs beautifully, and modulates nothing.

## Voice announcement

Also opt-in, via a `voice.enabled` flag file next to the script, and
only in the root beacon context. After the RF burst each cycle, the
station speaks on `voice_freq` (default 146.430 MHz FM simplex):
*"This is \<phonetic ID\>, \<place\>. The time is … Outdoor temperature
… degrees. All station systems normal."* The spoken ID satisfies the
FCC ID requirement for the transmission; content is amateur-directed
station information only, and of course no music, ever (Part 97).

**Rev 1.7:** the health line tells the truth about subsystems. If
the weather reading is stale or missing, the station announces
*"Weather system is down"* (and *"Web server is down"* if the site
check fails) — "All station systems normal" is spoken only when
every check actually passes.

**Rev 1.8:** the WX collector is paused (SIGSTOP) for the transmit window and resumed (SIGCONT) right after — on the single-core Pi Zero, rtl_433 streaming/decoding audibly corrupted the voice transmission (buzzing at the head of the announcement; found by A/B test, 2026-10-19).

## Device ID

The reference station uses the destination callsign **APZSHB**, from
the experimental APZ range, pending the APRS Foundation device
registry's decision (aprsorg/aprs-deviceid issue #381). If you run
your own build, set `tocall` in your config — the generic `APRS` is
always safe.

## Credits

- Original shellbeacon: **WA1GOV**
- Telemetry / weather / RF fork: **Robert McAtee, KJ5GBY**
- Built on Dire Wolf (WB2OSZ), rtl_433, and rpitx (F5OEO)

## License

This project is free software under the **GNU General Public License
v3.0** — see [LICENSE](LICENSE). You may run, study, share, and modify
it; if you distribute a modified version, it must carry the same
freedoms.

Provenance, stated plainly: the original shellbeacon 1.0 by WA1GOV was
shared publicly without stated license terms, and is credited above
and in the script header. The GPLv3 license covers this project —
Robert McAtee's fork, extensions, and `wx-collect.sh`.
