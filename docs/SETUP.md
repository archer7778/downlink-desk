# Setting up Downlink Desk on a Raspberry Pi Zero 2 W

About 30 minutes, most of it waiting for installs.

## 1. Get a SatNOGS DB API key

1. Create a free account at <https://db.satnogs.org> (it uses a Libre Space Foundation login).
2. Click your avatar (top right) → **Settings / API Token**, and copy the key.

Use the **DB** key, not a SatNOGS Network key; they're different, and the wrong one gives "401" errors.

## 2. Prepare the SD card

1. Install Raspberry Pi Imager on your computer (raspberrypi.com/software).
2. Device: **Raspberry Pi Zero 2 W**. OS: **Raspberry Pi OS Lite (64-bit)**.
3. In the customisation settings: hostname (e.g. `groundpi`), username and password, your Wi-Fi (2.4 GHz only) and country, and **enable SSH** with password login.
4. Optional backup way in over USB: before ejecting, open `user-data` on the card's boot drive and add
   ```
   rpi:
     enable_usb_gadget: true
   ```
   then connect the Pi's middle micro-USB port (nearest HDMI) to your computer with a data cable.

## 3. Connect

Power the Pi, wait 3–5 minutes, then from your computer:

```bash
ssh yourusername@groundpi.local
```

## 4. Install

```bash
sudo apt install -y git
git clone https://github.com/archer7778/downlink-desk.git downlink-desk
cd downlink-desk
./deploy/install.sh
```

It asks for your latitude and longitude (for pass predictions) and your API key. Installing the decoders takes a while on a Pi Zero.

## 5. Open the console

On any device on the same network: `http://groundpi.local:8080`. The first fetch runs within two minutes; or run

```bash
~/downlink-desk/.venv/bin/python -m downlink_desk.fetch --force
```

## Day to day

| Task | Command |
|---|---|
| See what's happening | `journalctl -u downlink-desk-web -u downlink-desk-fetch -f` |
| Restart after editing config.toml | `sudo systemctl restart downlink-desk-web` |
| Change the API key | `echo NEWKEY > ~/downlink-desk/secrets/satnogs_token` |
| Update Downlink Desk | `cd ~/downlink-desk && git pull && ./deploy/install.sh` |
| Back up | copy `~/downlink-desk/data/downlink-desk.db` somewhere else |

## Tracking more satellites

Click the satellite name at the top left of the console, then **Track another satellite**. Featured satellites have bios; the full list (everything in orbit that the installed decoders understand) downloads from SatNOGS once a day with the regular fetch. Tracking a satellite is signed in the audit trail and its data starts arriving within a couple of minutes.

Each tracked satellite adds a few requests to SatNOGS per fetch cycle. A handful is fine; please don't track dozens on a 20-minute cycle.

To give an automatic satellite proper names, units and limits, copy `channels/grbbeta.json` to `channels/<decoder>.json`, edit it, set the `channels` column for that satellite, and run `--rebuild`.

## Phone alerts (optional)

1. Install the free **ntfy** app (Android or iOS).
2. Subscribe to a topic name that only you know, e.g. `downlink-desk-7f3k9q`. Anyone who knows the name can read it, so make it hard to guess.
3. On the Pi, edit `config.toml` and set `ntfy_topic = "downlink-desk-7f3k9q"` under `[notify]`. Optionally set `console_url = "http://groundpi.local:8080"` so tapping an alert opens the console.
4. Nothing to restart; the next fetch uses it.

You'll get a push for alarm-level limit breaks, restarts, a silent run of scheduled observations, a channel drifting away from its learned normal, and three failed fetches in a row. Each event is sent once.

Every morning you also get a summary of the last 24 hours. It goes out with the first fetch after `summary_hour` (default 7) in `timezone`. The Pi's own clock is usually UTC, so set your zone, for example:

```
summary_hour = 7
timezone = "Asia/Ho_Chi_Minh"
```

Preview it any time with `.venv/bin/python -m downlink_desk.fetch --summary` (add `--send` to push it now).

## Starting at boot and updating itself

Nothing to do: the installer sets the console and the fetch job to start whenever the Pi powers up. It also installs an updater that checks GitHub 5 minutes after boot and every night at about 03:30. When there's a new version it pulls it, installs any new Python packages, restarts the console and checks it answers; if anything goes wrong it rolls back to the version that was running. It won't update while files in the repository have been edited on the Pi (it says so in the console instead), and it never touches `config.toml`, `secrets/` or `data/`.

- Update now: `sudo ~/downlink-desk/deploy/update.sh`
- Just check: `sudo ~/downlink-desk/deploy/update.sh --check`
- Turn automatic updates off: `auto_update = false` under `[updates]` in `config.toml`
- What it did: About tab in the console, or `journalctl -u downlink-desk-update`

The console's Ground segment shows the SD card's size, how full it is, the database size and roughly how long until it fills. You get a phone alert if it drops below 10% or 1 GB free.

## After updating

When an update changes units or adds orbit context, recalculate stored data once:

```bash
cd ~/downlink-desk && git pull && .venv/bin/python -m downlink_desk.fetch --rebuild && sudo systemctl restart downlink-desk-web
```

## Reaching it from outside your home

Don't open ports on your router. Install [Tailscale](https://tailscale.com) on the Pi and your phone/laptop instead (`curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up`), and use the Pi's Tailscale address. If teammates can reach it, set `write_passcode` in `config.toml`.

## Tips for a Pi Zero 2 W

- Use a proper 5 V 2.5 A power supply; weak chargers cause random crashes.
- Use a good SD card. The database uses WAL mode and small writes, but cards still wear; back up `data/` now and then.
- Please keep `poll_minutes` at 20 or more. SatNOGS is run by volunteers and throttles heavy users.

## Troubleshooting

- **Can't reach the Pi but it shows up in a network scan**: a VPN on your computer is probably blocking local traffic. Turn on its "allow LAN" option or disconnect it.
- **"SatNOGS rejected the API key"**: you used a Network key, or pasted it with extra characters. Re-save it.
- **No frames for hours**: normal if no volunteer station heard the satellite. Check its page on db.satnogs.org.
- **Lots of "Unrecognised" frames**: often digipeated amateur traffic or noise. Expected.
- **Values look wrong by a factor of 10 or 1000**: adjust that channel's `transform` in `channels/grbbeta.json`.
