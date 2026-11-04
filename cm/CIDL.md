# Configuration item data list

**Status:** DRAFT. Keyed by candidate; rekey to CI identifiers once `CIL.md` is decided.

ECSS-M-ST-40C Rev.1, DRD Annex C.

## 1. Introduction

The documents and data that define each candidate configuration item at commit `c5ac6bf`
(2026-11-04, version 0.17.1). The repository has no document numbers or issue letters of its
own; each file is identified by its path, and its issue by its git content ID (blob hash),
which changes whenever a single byte of the file changes. Two files with the same content ID
are identical.

## 2. Applicable and reference documents

- ECSS-M-ST-40C Rev.1, Annex C
- `CIL.md` for the candidates

## 3. List

### 3.1 Files in the repository

Generated from `git ls-tree -r -l c5ac6bf`. Last-changed commit IDs are those after the
history rewrite of 2026-11-04 (see `cm/README.md`).

| Cand. | File | Content ID (git blob) | Bytes | Last changed in | Date |
|---|---|---|---|---|---|
| CAND-01 | `downlink_desk/__init__.py` | `a756250f0edb` | 112 | `4e587e8` | 2026-10-12 |
| CAND-01 | `downlink_desk/analysis.py` | `ac73c8d02023` | 7,268 | `9d356f0` | 2026-10-12 |
| CAND-01 | `downlink_desk/baseline.py` | `880460adec9a` | 20,575 | `0558856` | 2026-10-12 |
| CAND-01 | `downlink_desk/battery.py` | `53fe3b90dd42` | 28,239 | `07bfab0` | 2026-10-12 |
| CAND-01 | `downlink_desk/catalog.py` | `6649b1f6eb90` | 4,635 | `0558856` | 2026-10-12 |
| CAND-01 | `downlink_desk/config.py` | `a8bd729fc0b3` | 3,871 | `3c0d38b` | 2026-10-12 |
| CAND-01 | `downlink_desk/db.py` | `04d02d1e232c` | 7,337 | `9b23f87` | 2026-10-11 |
| CAND-01 | `downlink_desk/decode.py` | `9d55d019fc57` | 3,430 | `3c0d38b` | 2026-10-12 |
| CAND-01 | `downlink_desk/export_static.py` | `78c9580ecc0c` | 4,836 | `07bfab0` | 2026-10-12 |
| CAND-01 | `downlink_desk/fetch.py` | `5a62e836516b` | 21,557 | `4e587e8` | 2026-10-12 |
| CAND-01 | `downlink_desk/forensics.py` | `5ac70aca2558` | 7,774 | `b4f4a05` | 2026-10-12 |
| CAND-01 | `downlink_desk/logchain.py` | `4f011058c446` | 3,012 | `a9b48d7` | 2026-10-11 |
| CAND-01 | `downlink_desk/notify.py` | `4e024e713baf` | 7,073 | `07bfab0` | 2026-10-12 |
| CAND-01 | `downlink_desk/orbit.py` | `6e6f6cb1907b` | 10,029 | `b4f4a05` | 2026-10-12 |
| CAND-01 | `downlink_desk/satnogs.py` | `7ea294446dc3` | 9,202 | `4e587e8` | 2026-10-12 |
| CAND-01 | `downlink_desk/server.py` | `01522a65fe9f` | 42,839 | `07bfab0` | 2026-10-12 |
| CAND-01 | `downlink_desk/spacewx.py` | `52eee25fe71d` | 6,065 | `0670bcf` | 2026-10-12 |
| CAND-01 | `downlink_desk/summary.py` | `4ebcc21221cf` | 9,563 | `07bfab0` | 2026-10-12 |
| CAND-02 | `web/index.html` | `e5ca525d5513` | 149,020 | `07bfab0` | 2026-10-12 |
| CAND-02 | `web/training.html` | `b0224037f386` | 94,108 | `edab7cc` | 2026-10-11 |
| CAND-02 | `web/world.json` | `b7f5167a2334` | 72,866 | `46bca9f` | 2026-10-11 |
| CAND-03 | `channels/grbbeta.json` | `42611e544dc4` | 7,091 | `07bfab0` | 2026-10-12 |
| CAND-04 | `catalog/featured.json` | `d8543224d635` | 3,373 | `a9b48d7` | 2026-10-11 |
| CAND-05 | `deploy/downlink-desk-fetch.service` | `7eb7329029ec` | 529 | `9d356f0` | 2026-10-12 |
| CAND-05 | `deploy/downlink-desk-fetch.timer` | `cb1412cb48f3` | 175 | `edc0e49` | 2026-10-11 |
| CAND-05 | `deploy/downlink-desk-update.service` | `b3d8f1acb5f6` | 269 | `b1cde69` | 2026-10-11 |
| CAND-05 | `deploy/downlink-desk-update.timer` | `1ef004fca57f` | 203 | `b1cde69` | 2026-10-11 |
| CAND-05 | `deploy/downlink-desk-web.service` | `f483a5265b77` | 548 | `0670bcf` | 2026-10-12 |
| CAND-05 | `deploy/install.sh` | `9d9067f811f1` | 4,070 | `0558856` | 2026-10-12 |
| CAND-05 | `deploy/update.sh` | `4d1245929ce7` | 5,176 | `4e587e8` | 2026-10-12 |
| CAND-06 | `config.example.toml` | `5fe307534f30` | 3,060 | `d46365e` | 2026-10-12 |
| CAND-06 | `demo-config.toml` | `a41cbecca12b` | 595 | `8c4aa85` | 2026-10-12 |
| CAND-07 | `tests/mock_satnogs.py` | `bc477d90c4c7` | 13,875 | `0558856` | 2026-10-12 |
| CAND-07 | `tests/schema_v01.sql` | `4ed8d8e35014` | 1,148 | `3bf7465` | 2026-10-12 |
| CAND-07 | `tests/test_pipeline.py` | `fa7bab8764c7` | 42,373 | `4e587e8` | 2026-10-12 |
| CAND-08 | `.github/workflows/demo.yml` | `fed59340ace4` | 3,733 | `c5ac6bf` | 2026-11-04 |
| CAND-09 | `README.md` | `7661101ac20b` | 15,780 | `8c4aa85` | 2026-10-12 |
| CAND-09 | `docs/SETUP.md` | `272921546d51` | 6,461 | `b1cde69` | 2026-10-11 |
| CAND-09 | `docs/index.html` | `b0224037f386` | 94,108 | `edab7cc` | 2026-10-11 |
| CAND-09 | `docs/screenshot.png` | `9049aecf3f01` | 300,738 | `276dec8` | 2026-10-11 |
| CAND-10 | `requirements.txt` | `a4e4e8ed699a` | 35 | `d37c800` | 2026-10-11 |
| CAND-12 | `.gitignore` | `f7d250e9a4a8` | 94 | `0633c73` | 2026-10-11 |
| CAND-12 | `LICENSE` | `c1c497ee48e9` | 1,082 | `7896804` | 2026-10-11 |

`docs/index.html` and `web/training.html` have the same content ID (`b0224037…`): `docs/index.html`
is an unused copy left from the first Pages demo (`edab7cc`); the demo now publishes
`web/training.html`. See `SCF.md`, section 12, item 6.

### 3.2 Documents held outside this repository

| Cand. | Document | Where | Identification |
|---|---|---|---|
| CAND-11 | Downlink Desk Operator's Manual (publication module `PMC-DLD-00000-00001-00`, 14 data modules) | `github.com/archer7778/downlink-desk-manual` | data module codes and issue numbers inside each module; applicability "Downlink Desk 0.17.x" |
| CAND-10 | satnogs-decoders | PyPI; source at `gitlab.com/librespacefoundation/satnogs/satnogs-decoders` | package version (1.132.0 in the last demo build) |
| CAND-10 | sgp4 | PyPI; source at `github.com/brandon-rhodes/python-sgp4` | package version (2.27 in the last demo build) |
| CAND-08 | Third-party GitHub Actions | `actions/checkout`, `actions/setup-python`, `actions/cache` (restore and save), `actions/upload-pages-artifact`, `actions/deploy-pages` | pinned to full commit IDs in `demo.yml` |
| — | External interface definitions | SatNOGS DB API, SatNOGS Network API, NOAA SWPC JSON services, ntfy | none controlled here; no interface control document exists |
