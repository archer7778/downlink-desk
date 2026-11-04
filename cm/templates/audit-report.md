# Audit report AR-___: configuration management system

**Status:** TEMPLATE.

An audit checks whether the CM system works as the plan says (ECSS-M-ST-40C Rev.1 calls for
audits of the CM system; this layout is the project's own). Configuration verification, which
compares as-built with as-designed, is a different activity, recorded in `ABCL.md`.

| | |
|---|---|
| Date | |
| Auditor | |
| Scope | e.g. all changes to `main` since BL-___ |
| Criteria | the CM plan, issue ___ |
| Evidence examined | `CSAR.md` at commit ___, issues, `ABCL.md`, … |

## Findings

Rate each: **major** (the CM system failed to control a change), **minor** (a record is wrong
or incomplete), **observation** (works, could be better).

| # | Rating | Finding | Evidence | Action (CR or RFW number) | Owner | Due |
|---|---|---|---|---|---|---|
| 1 | | | | | | |

## Questions to work through

1. Does every commit on `main` since the last baseline cite an approved CR? (`CSAR.md` 3.5)
2. Is every approved CR implemented, and every implemented change approved?
3. Do `CIL.md`, `CIDL.md` and `SCF.md` match the repository? Spot-check five content IDs.
4. Does each unit in `ABCL.md` match its as-designed configuration?
5. Are there unmerged branches with no CR? (`CSAR.md` 3.4)
6. Were interface changes (SatNOGS, decoders) noticed and handled?
7. Are secrets absent from every controlled item?
8. Did the metrics in `metrics.csv` move the right way since the last audit?

## Conclusion

Effective / effective with findings / not effective. Signed: ___
