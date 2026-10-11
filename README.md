# Downlink Desk

**A mission-control console for a real satellite, running on a $15 Raspberry Pi Zero 2 W.**

Downlink Desk watches real CubeSats in orbit. It pulls their telemetry from the [SatNOGS](https://satnogs.org) volunteer ground-station network, decodes it on the Pi, learns what normal looks like for each spacecraft, and runs a small operations console around it: alarms, a live feed, phone alerts, space weather and a signed audit trail. It uses almost none of the Pi: load average 0.00, about 150 MB of RAM, 1.4 MB of data for the first 200 frames.

![Downlink Desk console showing live GRBBeta telemetry from SatNOGS, running on a Raspberry Pi Zero 2 W](docs/screenshot.png)
*Live data: GRBBeta, a 2U CubeSat from Slovakia, as seen by a Pi Zero 2 W in Da Nang.*

## What it does

**Knows what normal looks like**
- **Learned thermal and power model.** A small satellite warms in sunlight and cools in Earth's shadow every 95 minutes. For each channel the Pi learns the typical value at each point of that cycle, corrects for the shadow growing and shrinking through the seasons, and compares the last two days with the three weeks before. A battery running 3 °C warmer than it used to at the same point in the orbit is still far inside its limits; this catches it anyway.
- **Orbit profile charts:** each channel plotted against minutes since sunrise, with the learned curve, its normal spread, and the latest readings on top.
- **Drift and oddity alerts** when a channel moves away from its own history, separate from the fixed limits.

**Checks the battery**
- **Battery health tab:** charge in and out around a whole orbit, how deep each eclipse drains the battery, whether it gets back to full in sunlight, voltage left at the end of eclipse and its trend, time spent outside lithium-ion's safe temperatures, and an effective internal resistance that rises as cells age.
- **Life estimate:** charge cycles since launch (about 12,000 for GRBBeta so far) against a cycle-life model for lithium-ion at the measured depth and temperature, giving an estimated capacity left and years to end of life, with a range. Battery details the team hasn't published are written down as assumptions, with their source, in one editable file.

**Watches the satellite and the network**
- **Live feed:** frames appear in every open console the moment the Pi stores them (server-sent events).
- **Expected vs received:** reads the SatNOGS Network schedule, so it knows which stations will listen and when, scores every observation as heard or empty, and counts down to the next data.
- **Silent-satellite alarm** when several scheduled observations in a row hear nothing, and fetch-health alerts when the Pi can't reach SatNOGS.
- **Limits and restarts:** caution and alarm limits per channel (or learned from the data in one click), and restart detection from the spacecraft's boot counters.

**Puts it in context**
- **Space weather from NOAA:** geomagnetic storms (Kp and the G1–G5 scale), solar flares from GOES X-ray data, and solar flux, marked on every channel's chart so you can see whether that odd reading lined up with a storm.
- **Where it is right now:** position, height, sunlight or shadow, and when the next sunrise is. Every frame is tagged with its orbit position, and the **South Atlantic Anomaly**, the radiation hot spot where spacecraft see the most particle hits, is flagged.
- **Passes over your own location**, worked out on the Pi with SGP4.

**Runs like a real operations desk**
- **Morning summary on your phone** through the free ntfy app: last night's frames, stations, restarts, drifts, storms and the day's best pass. Alarms, restarts and silence are pushed as they happen.
- **A signed audit trail:** every acknowledgement, shelve, limit change and handover is hash-chained on the Pi, so an edit or deletion shows up on verify.
- **Looks after itself:** starts at boot, updates itself from GitHub after boot and nightly (rolling back if the new version doesn't come up), and watches the SD card's free space.
- **Shift handover report, daily check, equipment status,** and a **training simulator** with fault injection.
- **Several satellites at once:** a picker with bios for featured CubeSats, and a catalog of every satellite in orbit that SatNOGS can decode.

> ⚠️ **Not flight software.** Downlink Desk is not affiliated with any satellite team, cannot command a spacecraft, and ships with provisional limits chosen for this console rather than the mission's own. Don't use it to make decisions about real hardware.

**Try the training simulator in your browser:** <https://archer7778.github.io/downlink-desk/> (once GitHub Pages is switched on for the `docs/` folder).

## How it works

```
volunteer SatNOGS stations ──► SatNOGS DB ──► Pi: fetch job (every 30 min) ──► SQLite ──► web console
                                              decodes with satnogs-decoders     │        on your network
NOAA space weather (every 3 h) ───────────────────────────────────────────────►┘        live feed (SSE)
                                              orbit phase + learned model ──► ntfy ──► your phone
```

- `downlink_desk/fetch.py` asks the SatNOGS DB API for new frames, politely: at least 11 s between requests, honours `Retry-After`, holds satellites flagged as frequency violators to one request a day.
- Frames are decoded locally with the community [satnogs-decoders](https://pypi.org/project/satnogs-decoders/) package and mapped to channels by a small JSON file.
- `downlink_desk/server.py` is a standard-library web server that serves the console and a JSON API, and signs audit-log entries.
- Scheduled observations come from the public SatNOGS Network API every 3 hours, one request.
- Sunlight and position come from the latest orbit data (SGP4 plus a cylindrical Earth-shadow model).
- Alerts are worked out in the browser from the stored telemetry, so every viewer sees the same thing. Phone alerts are checked by the fetch job on the Pi.
- `downlink_desk/baseline.py` is the learned model: robust medians in 5-minute bins of orbit phase, a seasonal correction for shadow length, and drift judged on effect size and significance together, so a single noisy pass can't trigger it.
- `downlink_desk/battery.py` places every frame in the orbit's day/night cycle and integrates charge and discharge current around it; internal resistance comes from how voltage moves with current once the orbit pattern is removed.
- `downlink_desk/spacewx.py` reads NOAA SWPC's public JSON products; `downlink_desk/summary.py` builds the morning summary (`python -m downlink_desk.fetch --summary` prints it).

The first satellite is **GRBBeta** (NORAD 60237), a 2U CubeSat from the Technical University of Košice testing a gamma-ray detector. Track more from the **satellite picker** in the console header: it lists every satellite in orbit that SatNOGS can decode, with hand-written bios for a featured few (CroCube, Foresail-1p, CatSat, CubeBel-2, Sharjahsat-1, UWE-4). Satellites without a curated channels file get automatic channels: every numeric field the decoder produces, in raw units.

## Install on a Raspberry Pi

You need Raspberry Pi OS Lite (64-bit), an internet connection, and a free SatNOGS DB account for the API key. Full walkthrough: [docs/SETUP.md](docs/SETUP.md).

```bash
sudo apt install -y git
git clone https://github.com/archer7778/downlink-desk.git downlink-desk
cd downlink-desk
./deploy/install.sh
```

The installer asks for your location and API key, installs two background services, and prints the console address (for example `http://groundpi.local:8080`).

## Develop and test

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python tests/test_pipeline.py           # fetch → decode → store → verify, against a mock SatNOGS
.venv/bin/python tests/mock_satnogs.py --port 8765 # offline demo data source
```

## Limitations

- Data arrives minutes to hours after a pass, only when a volunteer station heard the satellite and uploaded it. "Live" means live from the moment the Pi stores it.
- The health model needs about 25 samples per channel, and a few days of history, before it judges anything. The Pi fills in three weeks of older frames from SatNOGS a few pages at a time (or all at once with `python -m downlink_desk.fetch --history 21`). Orbit positions for old frames use the current orbit data, so frames more than 45 days older than it are left out of the model.
- The battery life figure is a model. GRBBeta's battery size and voltage scale are inferred from its platform builder's published power unit (20 Wh, about 8 V) and from the data itself, not confirmed by the team; correct them in `channels/grbbeta.json` if you know better.
- The South Atlantic Anomaly outline is an approximate ellipse; the real edge depends on altitude.
- Limits and some unit conversions (millivolts, kelvin) are inferred from decoder field names. Check them against the mission's documentation.
- The audit trail is tamper-evident, not tamper-proof: someone with access to the Pi's files could rebuild the chain.
- No user accounts. Run it on your home network or a private network such as Tailscale, and set `write_passcode` if others can reach it.

## Credits

- Telemetry: the [SatNOGS](https://satnogs.org) network, run by the [Libre Space Foundation](https://libre.space) and volunteer station operators. SatNOGS data is licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
- Decoding: [satnogs-decoders](https://pypi.org/project/satnogs-decoders/). Orbits: [python-sgp4](https://pypi.org/project/sgp4/).
- Space weather: [NOAA Space Weather Prediction Center](https://www.swpc.noaa.gov) (public domain).
- GRBBeta is operated by its own team: <https://grbbeta.tuke.sk/>.
- Built with help from Claude (Anthropic). Commits it made are marked as co-authored.

## License

MIT. See [LICENSE](LICENSE).
