# Software configuration file: Downlink Desk 0.17.1

**Status:** DRAFT. Not yet reviewed by the configuration manager.

ECSS-M-ST-40C Rev.1, DRD Annex E. Sections follow the DRD.

## 1. Introduction

Describes the software configuration item formed by the console software and the items it
needs to build, install and run (candidates CAND-01 to CAND-08 and CAND-10 in `CIL.md`; final
scope is the configuration manager's decision), at version 0.17.1, commit `c5ac6bf`.

## 2. Applicable and reference documents

| Ref. | Document |
|---|---|
| AD1 | ECSS-M-ST-40C Rev.1, Configuration and information management (6 March 2009) |
| RD1 | `cm/CIL.md`, `cm/CIDL.md`, `cm/ABCL.md` |
| RD2 | `README.md`, `docs/SETUP.md` |
| RD3 | Downlink Desk Operator's Manual, `github.com/archer7778/downlink-desk-manual` |

## 3. Terms, definitions and abbreviated terms

| Term | Meaning |
|---|---|
| Content ID | git blob hash of a file; identifies one exact content |
| Commit ID | git commit hash; identifies one exact state of the whole repository |
| Demo | the read-only copy of the console published to GitHub Pages by `demo.yml` |
| SatNOGS | Satellite Networked Open Ground Station, a network of volunteer ground stations |
| SWPC | NOAA Space Weather Prediction Center |
| TLE | two-line element set, the format of a satellite's orbit |

## 4. Software configuration item overview

A mission-operations console for small satellites. Every 30 minutes by default it fetches
decoded beacon frames from the SatNOGS DB API, decodes them with the community decoders, stores
them in SQLite, compares them with limits and a learned model of normal, predicts passes from
the TLE, checks whether scheduled observations delivered anything, and raises alerts in a web
console and optionally to a phone through ntfy. Operator actions are written to a hash-chained
audit trail. It runs on a Raspberry Pi Zero 2 W (per `docs/SETUP.md`) or another Debian, Ubuntu or Raspberry Pi OS machine (per `README.md`), and
a static export runs as the public demo.

## 5. Inventory of materials

Materials delivered with the item: none physical. The item is delivered as the git repository
at the commit above. See `CIDL.md` for every file with its content ID.

## 6. Baseline documents

No baseline has been established (no `BL-*` tag). The documents that would make up the first
baseline are listed in `CIDL.md`. **[CM]** to decide which baseline 0.17.1 belongs to, if any.

## 7. Inventory of software configuration item

| Component | Files | Content | Notes |
|---|---|---|---|
| Source code | `downlink_desk/*.py` | 18 modules, 4,251 lines | Python, standard library plus two packages |
| Web UI | `web/` | 3 files | served by `server.py`; copied into the demo by `export_static.py` |
| Data | `channels/grbbeta.json`, `catalog/featured.json` | 2 files | read at run time |
| Configuration | `config.example.toml`, `demo-config.toml` | 2 files | the live `config.toml` is per installation and not in the repository |
| Deployment | `deploy/` | installer, updater, 3 services, 2 timers | |
| Tests | `tests/` | 8 tests, mock SatNOGS server, v0.1 schema | |
| Build | `.github/workflows/demo.yml` | demo build | no build step for installations: they run from source |

Modules (first line of each docstring):

| Module | Purpose |
|---|---|
| `__init__` | Package marker and `__version__ = "0.17.1"` |
| `analysis` | Scheduled observations versus frames received; silence probability |
| `baseline` | Learned model of normal, channel by channel |
| `battery` | Battery health check from beacon telemetry |
| `catalog` | Which satellites can be followed |
| `config` | Configuration loading |
| `db` | SQLite storage (WAL mode) |
| `decode` | Decoding with the SatNOGS community decoders; mapping fields to channels |
| `export_static` | Read-only snapshot for the public demo |
| `fetch` | Fetch, decode and store new frames |
| `forensics` | Restart forensics |
| `logchain` | Hash-chained audit trail |
| `notify` | Phone alerts through ntfy |
| `orbit` | Pass prediction with SGP4 |
| `satnogs` | SatNOGS DB client |
| `server` | Web server and JSON API |
| `spacewx` | Space weather from NOAA SWPC |
| `summary` | Morning summary |

## 8. Means necessary for the software configuration item

| Need | Version | Source of the fact |
|---|---|---|
| Python | 3.11 or later | inferred from `import tomllib` in `config.py`; not stated in the repository |
| satnogs-decoders | 1.129 or later | `requirements.txt` |
| sgp4 | 2.23 or later | `requirements.txt` |
| sqlite3, python3-venv, python3-pip, git | from the OS | `deploy/install.sh`, `docs/SETUP.md` |
| systemd | from the OS | `deploy/*.service`, `deploy/*.timer` |
| Debian, Ubuntu or Raspberry Pi OS; the setup guide uses Raspberry Pi OS Lite (64-bit) on a Pi Zero 2 W | — | `README.md`, `docs/SETUP.md` |
| SatNOGS DB API key | — | `docs/SETUP.md`, step 1 |
| Demo build: GitHub Actions `ubuntu-24.04` runner, Python 3.12 | — | `demo.yml` |

## 9. Installation instructions

Follow `docs/SETUP.md`. In short: `git clone` the repository, then `./deploy/install.sh`, which
installs the OS packages, makes a virtual environment, installs `requirements.txt`, writes
`config.toml` if there isn't one, and enables `downlink-desk-web.service`,
`downlink-desk-fetch.timer` and `downlink-desk-update.timer`.

After installation, `downlink-desk-update.timer` runs `deploy/update.sh` 5 minutes after boot
and nightly at 03:30. The updater only fast-forwards: if the installed commit isn't an ancestor
of `origin/main` it stops with "Not updating: this Pi has its own commits. Merge by hand." With
`auto_update = false` it only reports that an update is available.

## 10. Change list

Releases are marked only by version numbers in commit subjects and in `__version__`; there are
no tags or release notes. Versions 0.6 to 0.12 have no commit naming them.

| Version | Commit | Date | Subject |
|---|---|---|---|
| 0.2 | `5cc918a` | 2026-10-11 | expected vs received, orbit context, learned limits, phone alerts |
| 0.3 | `a9b48d7` | 2026-10-11 | track several satellites, with a picker and bios |
| 0.4 | `9cb6915` | 2026-10-11 | learned health model, live feed, space weather, morning summary |
| 0.5 | `6381901` | 2026-10-11 | battery health check |
| 0.13.0 | `3bf7465` | 2026-10-12 | fuzz-hardened API and parsers, stale-TLE phase repair, v0.1 migration test |
| 0.14.0 | `0558856` | 2026-10-12 | failure-mode hardening (network, database, updater) and alert fixes |
| 0.15.0 | `3c0d38b` | 2026-10-12 | smarter restart forensics and silence maths, config validation |
| 0.16.0 | `9d356f0` | 2026-10-12 | clock guard, limit validation, DST-aware summary time |
| 0.16.1 | `b4f4a05` | 2026-10-12 | survive misdecoded values in the models |
| 0.17.0 | `07bfab0` | 2026-10-12 | clearer console and README |
| 0.17.1 | `4e587e8` | 2026-10-12 | history fill no longer crawls through upload floods; timers start without a reboot |

Changes since 0.17.1 without a version bump: `7f323f5` and `8c4aa85` (README and demo station,
2026-10-12) and `c5ac6bf` (demo workflow: keep the schedule on, pin Ubuntu 24.04, 2026-11-04).
None changes `downlink_desk/`.

## 11. Auxiliary information

- All commit IDs before 2026-11-04 were changed by a history rewrite on that day (links removed
  from commit messages). File contents and trees were not changed. Demo run 41823238853 was
  built at the old ID `216afd9`, whose tree is identical to `c5ac6bf`.
- 39 of the 40 commits on `main` name "Claude" as git author and committer, with a
  Claude co-author trailer. The latest, `c5ac6bf`, names Archie Maclean as author and committer
  with Claude as co-author.

## 12. Possible problems and known errors

| # | Problem | Evidence | Effect |
|---|---|---|---|
| 1 | Test `test_model_spaceweather_summary_stream` depends on the real date. It asserts the mock satellite's shadow is 25–40 minutes at now, now + 10 min and now − 5 days, but the mock orbit has seasons with short or no shadow. | Run on 2026-11-04 with Python 3.13 and satnogs-decoders 1.132.0, sgp4 2.27: 7 passed, 1 failed (shadow 14.0 min at now − 5 days). Shadow from `sun_phases` at 12:00 UTC: 33.5 min on 2026-10-12, 16.0 on 2026-10-20, 13.5 on 2026-10-30, 29.5 on 2026-11-04, none on 2027-01-15. | The suite fails on some days for reasons unrelated to the code, so a real failure can't be told from a seasonal one. |
| 2 | No workflow runs the tests. | `.github/workflows/` holds only `demo.yml`, which doesn't call pytest. | Changes reach `main`, the demo and installations untested unless someone runs the tests by hand. |
| 3 | Dependencies are minimum versions only. | `requirements.txt` | Two installs on different days can get different versions; an upstream decoder change can alter decoded values. |
| 4 | The 2026-11-04 history rewrite stops installed copies updating. | `deploy/update.sh` line 40: HEAD is not an ancestor of the rewritten `origin/main`. | Each installation reports "held" until fixed by hand. Number of affected installations not known. |
| 5 | Python version requirement is not stated. | Section 8 | An install on Python 3.10 or earlier fails at import with no explanation. |
| 6 | `docs/index.html` is an unused copy of `web/training.html`. | Same content ID in `CIDL.md`; added in `edab7cc` with the first Pages demo. The current demo publishes `web/training.html` through `export_static.py`, and nothing in the repository refers to `docs/index.html`. | A stale copy that can be mistaken for the live simulator. |
