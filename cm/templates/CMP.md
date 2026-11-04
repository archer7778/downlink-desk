# Configuration management plan: Downlink Desk

**Status:** TEMPLATE. To be written by the configuration manager.

Headings follow ECSS-M-ST-40C Rev.1, DRD Annex A. Under each heading, the *guidance* says what
the section has to settle, and *pointers* name the drafted records that hold relevant facts.
Delete the guidance as you write. Keep it short: this is a one-person project, and a plan that
says plainly "the configuration manager does this" is a better plan than one that invents a
board.

Document number: **[CM]** · Issue: **[CM]** · Date: **[CM]** · Approved by: **[CM]**

## 1. Introduction

*Guidance:* purpose and scope of the plan; which product and which life-cycle phases it covers;
what is out of scope (for example the runtime database and per-site `config.toml`).

## 2. Applicable and reference documents

*Guidance:* ECSS-M-ST-40C Rev.1 and anything else the plan depends on.

## 3. Management

### 3.1 Organization

*Guidance:* who the configuration manager is, who the "customer" is (for a class 1 change,
someone outside the developer has to approve it), and how an AI assistant's contributions fit:
who may propose, who may approve.

### 3.2 Responsibilities

*Guidance:* who raises CRs, classifies them, approves them, implements them, establishes
baselines, runs status accounting, and audits.

### 3.3 Policies, directives and procedures

*Guidance:* the rules you'll hold to. For example: every commit to `main` cites a CR; no history
rewrites on `main`; what needs a version bump.
*Pointers:* `cm/README.md` ("How a change moves").

### 3.4 Security

*Guidance:* how secrets (SatNOGS token, ntfy topic, write passcode) are kept out of the
controlled items; who has write access to the repository.
*Pointers:* `.gitignore` excludes `config.toml`, `secrets/`, `data/`; the demo token is the
repository secret `SATNOGS_TOKEN`.

## 4. Configuration management

### 4.1 General

*Guidance:* the approach in a paragraph, and the tools (git, GitHub issues, `tools/cm_status.py`).

### 4.2 Configuration identification

*Guidance:* which items are CIs and why; how they're identified (CI IDs, version numbers, tag
names); which baselines you'll establish and when. ECSS names functional, development and
product configuration baselines; decide what each would mean here, or which you'll use.
*Pointers:* `CIL.md`, `CI-selection-worksheet.md`, `CIDL.md`. No baseline exists yet.

### 4.3 Configuration control

*Guidance:* how a change is requested, assessed (including impact on installed copies),
classified (class 1 / class 2: define both for this project), approved and implemented; how
deviations and waivers are handled; emergency changes.
*Pointers:* `templates/CR.md`, `CP.md`, `RFD.md`, `RFW.md`; the issue form
`.github/ISSUE_TEMPLATE/change-request.yml`.

### 4.4 Interface control

*Guidance:* interfaces with things you don't control (SatNOGS DB and Network APIs, NOAA SWPC,
ntfy, satnogs-decoders field names), how a change on their side is noticed, and how it's
handled.
*Pointers:* `CIDL.md` section 3.2; `SCF.md` section 12, item 3.

### 4.5 Supplier control

*Guidance:* the third-party packages and GitHub Actions: how versions are chosen and pinned,
who checks new versions.
*Pointers:* `requirements.txt` (minimums only); `demo.yml` (actions pinned to commit IDs).

### 4.6 Configuration status accounting

*Guidance:* what is recorded, where, and how often.
*Pointers:* `tools/cm_status.py` writes `CSAR.md` and `metrics.csv`.

### 4.7 Configuration verification

*Guidance:* how you confirm what's running matches what's approved, and when.
*Pointers:* `ABCL.md` (demo recorded; Pi TBD).

### 4.8 Audits of the configuration management system

*Guidance:* when you audit the CM system itself, against what criteria, and what happens to
findings.
*Pointers:* `templates/audit-report.md`.

### 4.9 Technical data management

*Guidance:* how documents (README, setup guide, operator's manual in its own repository) are
kept in step with the software.

## 5. Information and documentation management

### 5.1 Information identification

*Guidance:* how documents are identified and versioned (path plus git content ID? document
numbers?).

### 5.2 Data formats

*Guidance:* Markdown, TOML, JSON, the manual's XML.

### 5.3 Processes

*Guidance:* how a document is drafted, reviewed and released.

### 5.4 Information systems

*Guidance:* GitHub (repository, issues, Actions, Pages).

### 5.5 Delivery methods

*Guidance:* how installations and readers receive the product: git clone and the self-updater;
GitHub Pages for the demo and the manual.

### 5.6 Digital signature

*Guidance:* whether commits or tags are signed. Not to be confused with the console's
hash-chained audit trail.

### 5.7 Information status reporting

*Guidance:* how document status is reported.
*Pointers:* each record under `cm/` has a **Status:** line, which `cm_status.py` reads.

## 6. Schedule and resources

### 6.1 Schedule

*Guidance:* when the first baseline is established, when audits happen.

### 6.2 Resources

*Guidance:* people and time. Be honest: one person, a few hours a week.
