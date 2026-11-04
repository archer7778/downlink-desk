#!/usr/bin/env python3
"""Configuration status accounting for Downlink Desk (ECSS-M-ST-40C, DRD Annex F).

Reads the git history and writes two files:

  cm/CSAR.md      the configuration status accounting report
  cm/metrics.csv  one row per run, appended, for tracking the CM system over time

Sources, all read-only:
  - baseline tags named BL-* (for example BL-FCB-1, BL-0.17.1); none yet is reported as such
  - CR-### references in commit messages (for example "CR-004: pin requirements")
  - branches not merged into main (proposed changes with no record)
  - the documents under cm/ and their last change
  - with --issues, change requests raised as GitHub issues labelled change-request (needs gh)

Standard library only. Run from anywhere inside the repository:

  python3 tools/cm_status.py            # write the report and append a metrics row
  python3 tools/cm_status.py --issues   # also read change requests from GitHub
  python3 tools/cm_status.py --check    # print the report, write nothing
"""
import argparse
import os
import csv
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

CR_RE = re.compile(r"\bCR-(\d{3,})\b")
MAIN = "main"


def git(*args, ok_fail=False):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        if ok_fail:
            return ""
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def baselines():
    out = git("for-each-ref", "refs/tags/BL-*", "--sort=creatordate",
              "--format=%(refname:short)\t%(objectname)\t%(*objectname)\t%(creatordate:short)\t%(subject)")
    rows = []
    for line in filter(None, out.splitlines()):
        name, obj, peeled, date, subject = (line.split("\t") + [""] * 5)[:5]
        commit = peeled or obj          # annotated tags point at a tag object; use the commit
        rows.append({"name": name, "commit": commit[:7], "date": date, "subject": subject})
    return rows


def commits(since=None):
    rng = f"{since}..{MAIN}" if since else MAIN
    out = git("log", rng, "--format=%h\x1f%cs\x1f%an\x1f%s\x1f%b\x1e", ok_fail=True)
    rows = []
    for rec in filter(None, (r.strip() for r in out.split("\x1e"))):
        h, date, author, subject, body = (rec.split("\x1f") + [""] * 5)[:5]
        refs = sorted(set(CR_RE.findall(subject + "\n" + body)), key=int)
        rows.append({"hash": h, "date": date, "author": author, "subject": subject,
                     "crs": [f"CR-{n}" for n in refs]})
    return rows


def unmerged_branches():
    out = git("branch", "-a", "--no-merged", MAIN, "--format=%(refname:short)", ok_fail=True)
    seen, rows = set(), []
    for b in filter(None, out.splitlines()):
        short = b.removeprefix("origin/")
        if short in seen or short in ("HEAD", MAIN) or b.endswith("/HEAD"):
            continue
        seen.add(short)
        ahead = git("rev-list", "--count", f"{MAIN}..{b}", ok_fail=True) or "?"
        last = git("log", "-1", "--format=%cs", b, ok_fail=True)
        rows.append({"branch": short, "ahead": ahead, "last": last,
                     "crs": sorted(set(CR_RE.findall(git("log", f"{MAIN}..{b}", "--format=%B", ok_fail=True))))})
    return rows


def cm_documents(root):
    rows = []
    for p in sorted((root / "cm").rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if rel == "cm/CSAR.md":
            continue
        last = git("log", "-1", "--format=%h\x1f%cs", "--", rel, ok_fail=True)
        h, date = (last.split("\x1f") + ["", ""])[:2] if last else ("uncommitted", "")
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^\*\*Status:\*\*\s*(.+)$", text, re.M)
        rows.append({"path": rel, "commit": h, "date": date, "status": m.group(1).strip() if m else "not stated"})
    return rows


def issues():
    # REST rather than `gh issue list` (GraphQL), so it works wherever gh can reach the REST API.
    try:
        r = subprocess.run(["gh", "api", "--paginate",
                            "repos/{owner}/{repo}/issues?labels=change-request&state=all&per_page=100"],
                           capture_output=True, text=True)
    except FileNotFoundError:
        return None, "gh is not installed"
    if r.returncode != 0:
        return None, (r.stderr.strip().splitlines() or ["gh failed"])[0]
    try:
        # --paginate concatenates one JSON array per page
        raw = json.loads("[" + r.stdout.strip().replace("][", "],[") + "]") if r.stdout.strip() else []
        items = [i for page in raw for i in page if "pull_request" not in i]
    except (ValueError, TypeError):
        return None, "unexpected reply from gh"
    rows = [{"number": i["number"], "title": i["title"], "state": i["state"].upper(),
             "createdAt": i.get("created_at") or "", "closedAt": i.get("closed_at") or ""} for i in items]
    return sorted(rows, key=lambda i: i["number"]), None


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def build(root, want_issues):
    today = dt.date.today().isoformat()
    head = git("rev-parse", "--short", MAIN)
    bls = baselines()
    last_bl = bls[-1] if bls else None
    since = git("rev-list", "-n1", last_bl["name"]) if last_bl else None
    cs = commits(since)
    flagged = [c for c in cs if not c["crs"]]
    br = unmerged_branches()
    docs = cm_documents(root)
    iss, iss_err = issues() if want_issues else (None, "not requested (run with --issues)")

    L = [f"# Configuration status accounting report",
         "",
         f"**Status:** generated by `tools/cm_status.py` on {today} from `{MAIN}` at `{head}`. Do not edit by hand.",
         "",
         "ECSS-M-ST-40C, DRD Annex F. Sections follow the DRD: introduction, applicable and reference",
         "documents, description.",
         "",
         "## 1. Introduction",
         "",
         "What is approved, what is proposed, and what is implemented, read from the repository.",
         "",
         "## 2. Applicable and reference documents",
         "",
         "- ECSS-M-ST-40C Rev.1, Configuration and information management",
         "- cm/README.md (how these records map to the standard)",
         "",
         "## 3. Description",
         "",
         "### 3.1 Baselines (record of approved configuration)",
         ""]
    if bls:
        L.append(md_table(["Tag", "Commit", "Date", "Subject"],
                          [[b["name"], b["commit"], b["date"], b["subject"]] for b in bls]))
    else:
        L += ["**No baseline yet.** No `BL-*` tag exists, so nothing is approved and every commit on",
              f"`{MAIN}` is outside configuration control. Establishing the first baseline is the",
              "configuration manager's decision (see cm/templates/CMP.md, section 4.2)."]
    L += ["", "### 3.2 Documents index and status list", ""]
    L.append(md_table(["Document", "Last commit", "Date", "Status"],
                      [[d["path"], d["commit"], d["date"], d["status"]] for d in docs]) if docs else "None.")
    L += ["", "### 3.3 Change requests", ""]
    if iss is None:
        L.append(f"GitHub issues not read: {iss_err}.")
    elif not iss:
        L.append("No issues labelled `change-request`.")
    else:
        L.append(md_table(["Issue", "Title", "State", "Raised", "Closed"],
                          [[f"#{i['number']}", i["title"], i["state"].lower(), i["createdAt"][:10],
                            (i.get("closedAt") or "")[:10]] for i in iss]))
    cited = sorted({cr for c in cs for cr in c["crs"]}, key=lambda s: int(s[3:]))
    L += ["", f"CR numbers cited in commits {'since ' + last_bl['name'] if last_bl else 'on ' + MAIN}: "
          + (", ".join(cited) if cited else "none") + "."]
    L += ["", "### 3.4 Proposed changes not yet merged", ""]
    L.append(md_table(["Branch", "Commits ahead of main", "Last commit", "CRs cited"],
                      [[b["branch"], b["ahead"], b["last"], ", ".join(f"CR-{n}" for n in b["crs"]) or "**none**"]
                       for b in br]) if br else "None.")
    L += ["", "### 3.5 Implementation status", "",
          f"Commits on `{MAIN}` {'since ' + last_bl['name'] if last_bl else '(whole history, no baseline)'}: "
          f"{len(cs)}. With a CR reference: {len(cs) - len(flagged)}. Without: {len(flagged)}.", ""]
    if flagged:
        L += ["Commits without a CR reference (each is either a change made outside control, or needs a",
              "retrospective CR or a recorded decision that it didn't need one):", ""]
        L.append(md_table(["Commit", "Date", "Author", "Subject"],
                          [[c["hash"], c["date"], c["author"], c["subject"]] for c in flagged]))
    L += ["", "### 3.6 Indexes not yet in use", "",
          "Drawing index, RFD index, RFW index, CP index and RID index (Annex F, 2.1c): none raised.",
          "Add them here when the first one is raised.", ""]
    report = "\n".join(L)

    metrics = {"date": today, "head": head, "baselines": len(bls),
               "commits_since_baseline": len(cs), "commits_with_cr": len(cs) - len(flagged),
               "commits_without_cr": len(flagged), "unmerged_branches": len(br),
               "crs_open": "" if iss is None else sum(i["state"] == "OPEN" for i in iss),
               "crs_closed": "" if iss is None else sum(i["state"] != "OPEN" for i in iss)}
    return report, metrics


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--issues", action="store_true", help="read change requests from GitHub issues (needs gh)")
    ap.add_argument("--check", action="store_true", help="print the report; write nothing")
    a = ap.parse_args()
    root = pathlib.Path(git("rev-parse", "--show-toplevel"))

    os.chdir(root)
    if not git("rev-parse", "--verify", "-q", MAIN, ok_fail=True):
        sys.exit(f"no branch called {MAIN}")
    report, metrics = build(root, a.issues)
    if a.check:
        print(report)
        return
    (root / "cm").mkdir(exist_ok=True)
    (root / "cm" / "CSAR.md").write_text(report + "\n", encoding="utf-8")
    mpath = root / "cm" / "metrics.csv"
    new = not mpath.exists()
    with mpath.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(metrics))
        if new:
            w.writeheader()
        w.writerow(metrics)
    print(f"Wrote cm/CSAR.md and a row in cm/metrics.csv ({metrics['commits_without_cr']} commits without a CR reference).")


if __name__ == "__main__":
    main()
