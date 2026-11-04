# As-built configuration list

**Status:** DRAFT. Demo recorded from its build log; Raspberry Pi not yet recorded.

ECSS-M-ST-40C Rev.1, DRD Annex D.

## 1. Introduction

What is actually running, unit by unit, so it can be compared with the as-designed configuration
in `CIDL.md` during configuration verification. There are two kinds of unit: the public demo on
GitHub Pages, and installations on a Raspberry Pi.

## 2. Applicable and reference documents

- ECSS-M-ST-40C Rev.1, Annex D
- `CIDL.md` (as designed), `SCF.md`

## 3. List

### 3.1 Unit DEMO: public demo, archer7778.github.io/downlink-desk

Recorded from the log of GitHub Actions run 41823238853.

| Item | As built | As designed (`CIDL.md`) | Match |
|---|---|---|---|
| Source commit | `216afd9` (ID before the 2026-11-04 rewrite) | `c5ac6bf` | Same tree, `5c7452007e15c60feeb288808dc473123372871e` |
| Build time and trigger | 2026-11-04 11:43 UTC, triggered by a push (so the SatNOGS fetch step was skipped by design) | — | — |
| Runner OS | Ubuntu 24.04.5 LTS, runner image `ubuntu-24.04` version 20261025.353.1 | `runs-on: ubuntu-24.04` | yes |
| Python | CPython 3.12.15 | `python-version: "3.12"` | yes |
| satnogs-decoders | 1.132.0 | 1.129 or later | within range |
| sgp4 | 2.27 | 2.23 or later | within range |
| kaitaistruct | 0.10 | not specified (dependency of satnogs-decoders) | — |
| versioneer | 0.28 | not specified (dependency of satnogs-decoders) | — |
| Configuration | `demo-config.toml` at `a41cbecc…` | same | yes |
| Data | restored from Actions cache key `demo-db-41786618340`, i.e. the database saved by run 41786618340 (not version-controlled) | — | not verifiable |

To refresh this record, open the latest successful "Live demo" run and read the setup and
install steps of its log.

### 3.2 Unit PI-01: Raspberry Pi installation

**TBD [CM].** Not reachable while drafting. To record it, run on the Pi:

```bash
cd ~/downlink-desk
git rev-parse HEAD; git rev-parse 'HEAD^{tree}'; git status --short
.venv/bin/python --version
.venv/bin/pip freeze
cat data/update.json
cat /etc/os-release | head -3
diff <(grep -v '^#' config.example.toml) <(grep -v '^#' config.toml)   # check no secrets before sharing
```

Expected at the time of drafting: the Pi holds a pre-rewrite commit ID, so `data/update.json`
shows `"result":"held"`. Record its tree ID: if it matches a tree on `main`, the code is as
designed even though the commit ID differs.

| Item | As built | As designed | Match |
|---|---|---|---|
| Hardware | | Raspberry Pi Zero 2 W (`docs/SETUP.md`) | |
| OS | | Raspberry Pi OS Lite (64-bit) | |
| Commit / tree | | `c5ac6bf` / `5c745200…` | |
| Local changes | | none | |
| Python | | 3.11 or later | |
| Packages | | `requirements.txt` | |
| Updater status | | `current` | |
| Configuration | | `config.example.toml` plus site values | |
