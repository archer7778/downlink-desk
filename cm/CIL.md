# Configuration item list

**Status:** DRAFT. Candidate inventory; selection not yet made.

ECSS-M-ST-40C, DRD Annex B.

## 1. Introduction

Every part of Downlink Desk that could be a configuration item, with the facts needed to decide.
The configuration manager decides which are selected, gives each selected item its final
identifier, and writes the rationale (use `CI-selection-worksheet.md`). Columns marked **[CM]**
are his.

Candidate IDs (`CAND-nn`) are working labels only, not CI identifiers.

## 2. Applicable and reference documents

- ECSS-M-ST-40C Rev.1, Annex B (CI list) and Annex K (CI selection, informative)
- `CIDL.md` (documents for each item), `SCF.md` (software item detail)

## 3. List

Repository: `github.com/archer7778/downlink-desk`, commit `c5ac6bf` (2026-11-04), version
`0.17.1` (`downlink_desk/__init__.py`).

| Cand. | Name | What it is | Files | Developed or bought | Interfaces | Selected? **[CM]** | CI ID **[CM]** | Rationale **[CM]** |
|---|---|---|---|---|---|---|---|---|
| CAND-01 | Console software | Python package: fetch, decode, models, alerts, audit trail, web server, static export | `downlink_desk/` (18 modules, 4,251 lines) | Developed | SatNOGS DB and Network APIs, NOAA SWPC, ntfy, SQLite file, HTTP API to the web UI | | | |
| CAND-02 | Web console | Single-page operator UI, training simulator, world map data | `web/index.html`, `web/training.html`, `web/world.json` | Developed; map outlines from Natural Earth (public domain) via world-atlas, per README | HTTP JSON API and event stream of CAND-01 | | | |
| CAND-03 | Channel definitions | Which decoded fields show, with units and limits | `channels/grbbeta.json` | Developed | Read by CAND-01; field names from satnogs-decoders | | | |
| CAND-04 | Satellite catalogue | Featured satellites and bios | `catalog/featured.json` | Developed | Read by CAND-01 | | | |
| CAND-05 | Deployment kit | Installer, self-updater, systemd units | `deploy/` (7 files) | Developed | Raspberry Pi OS, systemd, git, GitHub | | | |
| CAND-06 | Configuration templates | Example config for a Pi; config for the public demo | `config.example.toml`, `demo-config.toml` | Developed | Read by CAND-01 | | | |
| CAND-07 | Test suite | End-to-end tests with a mock SatNOGS server | `tests/` (3 files, 8 tests) | Developed | Runs CAND-01 | | | |
| CAND-08 | Demo pipeline | GitHub Actions workflow that fetches real data and publishes the demo | `.github/workflows/demo.yml` | Developed; uses 5 third-party actions pinned to commit IDs | GitHub Actions, GitHub Pages, SatNOGS DB API (secret `SATNOGS_TOKEN`) | | | |
| CAND-09 | User documentation | README, Pi setup guide, screenshot, and `docs/index.html` (an unused, byte-identical copy of `web/training.html`) | `README.md`, `docs/` | Developed | — | | | |
| CAND-10 | Third-party Python packages | satnogs-decoders, sgp4 (satnogs-decoders pulls in kaitaistruct and versioneer) | `requirements.txt` (minimum versions) | Bought in (open source) | Imported by CAND-01 | | | |
| CAND-11 | Operator's manual | S1000D-style manual for this console | separate repository `archer7778/downlink-desk-manual` | Developed | Describes CAND-01, CAND-02, CAND-05 | | | |
| CAND-12 | Repository housekeeping | Licence and ignore rules | `LICENSE`, `.gitignore` | Developed | — | | | |

Not candidates, recorded so nothing is lost:

- **Deployed installations** (a Raspberry Pi Zero 2 W per `docs/SETUP.md`, and the GitHub Pages
  demo) are instances of the items above, not items. They belong in `ABCL.md`.
- **Runtime data** (`data/`, the SQLite database, `secrets/`, `config.toml`) is excluded by
  `.gitignore` and is not under version control.
- **Branch `lattice-bridge`** is a proposed change, not an item. It appears in `CSAR.md`.
