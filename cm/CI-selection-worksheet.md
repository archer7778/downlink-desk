# CI selection worksheet

**Status:** BLANK. For the configuration manager to complete.

Based on ECSS-M-ST-40C Rev.1, Annex K (informative), the checklist for selecting configuration
items. Answer each question for each candidate in `CIL.md` (Y, N or ?). The standard doesn't
give a scoring rule; a candidate with several "Y" answers is a strong CI, but the decision and
its reasons are yours. Write the reason in the last row, then copy the decision into `CIL.md`.

Question text is paraphrased; check the standard for the exact wording.

| | Question | 01 Console sw | 02 Web | 03 Channels | 04 Catalogue | 05 Deploy | 06 Config | 07 Tests | 08 Demo pipeline | 09 User docs | 10 3rd-party pkgs | 11 Manual | 12 Housekeeping |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| a | Is it critical for safety, schedule or finance? | | | | | | | | | | | | |
| b | Is it a new design? | | | | | | | | | | | | |
| c | Does it use new technologies? | | | | | | | | | | | | |
| d | Does it interface with hardware or software developed under another contract? | | | | | | | | | | | | |
| e | Does it need logistic support? | | | | | | | | | | | | |
| f | Does it share common requirements and testing with other items? | | | | | | | | | | | | |
| g | Are its functions interdependent with others? | | | | | | | | | | | | |
| h | Can it be reconfigured? | | | | | | | | | | | | |
| i | Is it procured separately? | | | | | | | | | | | | |
| j | Is it tested independently? | | | | | | | | | | | | |
| k | Is it readily identifiable? | | | | | | | | | | | | |
| l | Does it interface with CIs controlled by others? | | | | | | | | | | | | |
| m | Do you need to know its exact configuration and changes over the life cycle? | | | | | | | | | | | | |
| n | Does it have separate mission, training, test or maintenance functions? | | | | | | | | | | | | |
| | **Decision (CI / not a CI / part of another CI)** | | | | | | | | | | | | |
| | **Reason** | | | | | | | | | | | | |

## Facts that may help (not answers)

- **01, 02:** CAND-02 only works with CAND-01's HTTP API (`/api/*` and the `/api/stream` event
  stream); the static demo exports the same API as JSON files.
- **03, 10:** the channel field names come from the satnogs-decoders package (`grbbeta`
  decoder). A decoder change upstream can break a channel definition without any change here.
- **05:** `deploy/update.sh` fast-forwards installed copies from GitHub nightly and after boot, when `auto_update` is true (the default in `config.example.toml`).
  What it receives is whatever `main` holds, so changes to `main` reach installations without
  any other release step.
- **07:** no workflow runs the tests; they're run by hand.
- **08:** runs on a schedule (every 6 hours) against the live SatNOGS API, so it is used in
  operation, not just in development.
- **10:** controlled by others (satnogs-decoders by the SatNOGS project of the Libre Space Foundation; sgp4 by Brandon Rhodes).
  `requirements.txt` pins minimums only.
- **11:** lives in its own repository with its own history; its applicability statement is
  "Downlink Desk 0.17.x".
