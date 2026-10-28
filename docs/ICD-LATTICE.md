# ICD-L: Downlink Desk ↔ Anduril Lattice

**Interface control document for the Lattice bridge** (`downlink_desk/lattice.py`)
Status: draft, built and verified against a mock Lattice server and the official SDK's types (`anduril-lattice-sdk` 5.3.0). Not yet run against a Lattice Sandbox (see §9, open items).

## 1. Purpose and scope

Downlink Desk is a ground-segment operations console that runs on a small Linux computer (a Raspberry Pi Zero 2 W in the reference install). It monitors satellites through the SatNOGS volunteer ground-station network.

This interface lets it take part in a Lattice mesh as an edge node:

- It **publishes** each tracked satellite, the Downlink Desk node itself, and the SatNOGS stations due to hear a satellite, as Lattice **entities**, with health, alerts, orbit and position.
- It **accepts one task** from Lattice, `ExpeditePass`: an operator asks the node to collect a specific pass's data as soon as it's uploaded, and the node reports back through the standard task lifecycle.

It does **not** command any spacecraft or ground station. Downlink Desk has no uplink. The only action a task triggers is a more frequent, still rate-limited, fetch from SatNOGS.

## 2. Context

```
 SatNOGS stations ──► SatNOGS DB / Network ──► fetch job ──► SQLite ──► console (LAN)
                                                                │
                                         read-only ─────────────┘
                                                                ▼
                                                     Lattice bridge (this ICD)
                                                       │  outbox.db (own file)
                                     ICD-L1 entities ──┤
                                     ICD-L2 tasks ◄────┤  REST / HTTPS, bearer token
                                     ICD-L3 status ────┘
                                                                ▼
                                                     Lattice (Sandbox or deployment)
```

The bridge is a separate process (`downlink-desk-lattice.service`). It reads the console's database without writing to it, and keeps its own queue and task state in a second SQLite file. If the bridge stops, crashes or loses its link, the console, alarms, phone alerts and fetches carry on unchanged. This is the local-first principle in Lattice's own developer guidance: processing stays at the edge, and the mesh link is additive.

## 3. Interface summary

| ID | Direction | What | Lattice call | Rate |
|---|---|---|---|---|
| ICD-L1 | Bridge → Lattice | Entity publish: satellites, node, stations | `PUT /api/v1/entities` | Every 30 s (satellites, node); every 5 min (stations) |
| ICD-L2 | Lattice → Bridge | Task delivery: execute, cancel, complete | `POST /api/v1/agent/listen` (long poll, ≤5 min hold) | Continuous |
| ICD-L3 | Bridge → Lattice | Task status updates | `PUT /api/v1/tasks/{id}/status`, `GET /api/v1/tasks/{id}` to resync | On each state change |
| ICD-L4 | Bridge → Lattice | Authentication | Bearer environment token, or `POST /api/v1/oauth/token` (client credentials); optional `Anduril-Sandbox-Authorization` header | On start and before token expiry |

Transport is HTTPS with JSON, using the REST field names generated in the SDK (camelCase). The bridge uses Python's standard library rather than the SDK, so it adds no dependencies on the Pi; the test suite checks every payload against the SDK's own models (§8, LAT-T01).

## 4. Entities (ICD-L1)

### 4.1 Identity

Entity IDs are UUIDv5 values derived from a node ID that is generated once per installation and stored in `outbox.db`:

| Entity | ID seed |
|---|---|
| Satellite | `downlink-desk:{node}:satellite:{NORAD}` |
| Node | `downlink-desk:{node}:node` |
| SatNOGS station | `downlink-desk:{node}:satnogs-station:{station_id}` |

The same installation therefore always updates the same entities across restarts, and two installations watching the same satellite publish separate entities. Both carry the NORAD catalogue number as an alias (`ALT_ID_TYPE_NORAD_CAT_ID`), so Lattice correlation can merge them.

### 4.2 Satellite entity (one per tracked satellite)

| Component | Content | Source in Downlink Desk |
|---|---|---|
| `ontology` | `TEMPLATE_ASSET`, platformType `Satellite`, specificType from the catalog (for example `2U CubeSat`) | `catalog/featured.json` |
| `milView` | `DISPOSITION_NEUTRAL`, `ENVIRONMENT_SPACE` | Fixed: a civil satellite this node watches but doesn't operate |
| `aliases` | Name; `ALT_ID_TYPE_NORAD_CAT_ID` | `satellites` table |
| `location` | Geodetic WGS84 latitude, longitude, height above ellipsoid; velocity in ENU; speed | SGP4 from the latest TLE, at publish time |
| `orbit.orbitMeanElements` | Mean Keplerian elements, TLE parameters, TEME frame, SGP4 theory, originator and creation date | Latest TLE from SatNOGS DB |
| `health` | Roll-up status, one component per subsystem plus a Downlink component, active alerts (§4.5) | Latest telemetry, limits in force, learned model, restart forensics, silence check |
| `status.platformActivity` | For example `SUNLIT · NEXT DOWNLINK 14:32Z PISZKESTETO` | Orbit state, observation schedule |
| `relationships` | `trackedBy` a station entity while it is observing, or for 30 min after it heard the satellite | Observation report |
| `provenance` | integrationName `downlink-desk`, dataType `SatNOGS telemetry + SGP4`, sourceId `norad:{n}`, sourceUpdateTime | Publish time |
| `dataClassification` | `UNCLASSIFIED` | All inputs are public data |
| `expiryTime` | Send time + 15 min | Config `expiry_minutes` |

**Position is an estimate, not a measurement.** SGP4 from a TLE is typically good to a few kilometres near the TLE epoch, worsening by roughly a few kilometres per day of TLE age. The description states the TLE age.

**TLE derivative terms follow the CCSDS OMM convention** that Lattice's orbit component cites: `meanMotionDot` and `meanMotionDdot` carry the TLE fields as written (ṅ/2 and n̈/6), not rescaled.

### 4.3 Node entity (the Downlink Desk installation)

| Component | Content |
|---|---|
| `ontology` | `TEMPLATE_ASSET`, platformType `Ground Station`, specificType `Downlink Desk {version}` |
| `milView` | `DISPOSITION_FRIENDLY`, `ENVIRONMENT_LAND` |
| `location` | Station latitude and longitude, **rounded to 0.1° (about 10 km) by default**; `exact` or `none` by config. No altitude: the config holds height above sea level, and Lattice wants height above the ellipsoid, which needs a geoid model the Pi doesn't carry |
| `health` | Components: SatNOGS DB link, observation schedule, storage, Lattice link (queue depth and age), software version and update state |
| `status.platformActivity` | `MONITORING 2 SATELLITES` or `EXPEDITING GRBBETA PASS 13421188` |
| `taskCatalog` | One definition: `type.googleapis.com/downlinkdesk.v1.ExpeditePass` |

The node is the taskable agent: tasks are addressed to its entity ID.

### 4.4 SatNOGS station entities

One per SatNOGS station with an observation of a tracked satellite that is under way, starts in the next 12 hours, or ended in the last 30 minutes.

| Component | Content |
|---|---|
| `ontology` | `TEMPLATE_ASSET`, platformType `Ground Station`, specificType `SatNOGS station` |
| `milView` | `DISPOSITION_NEUTRAL`, `ENVIRONMENT_LAND` |
| `aliases` | `SatNOGS {id} {name}`; `ALT_ID_TYPE_ASSET_ID` = `satnogs-station-{id}` |
| `location` | Latitude and longitude from the public SatNOGS Network schedule |
| `status.platformActivity` | `OBSERVING GRBBETA`, `SCHEDULED GRBBETA 14:32–14:41Z EL 54°` or `HEARD GRBBETA 14:35Z` |
| `expiryTime` | 30 min after its last relevant observation ends (never more than 30 days ahead) |

Stations carry **no health component**. Downlink Desk doesn't know a volunteer station's health; it knows only that station's track record of hearing the satellite, which goes in the description.

### 4.5 Health and alert mapping

Limits are the four values the console uses: alarm low, caution low, caution high, alarm high.

| Condition (from the console's own logic) | Lattice alert | Level | Component health |
|---|---|---|---|
| Latest value outside alarm limits | `LIMIT_ALARM:{channel}` | `ALERT_LEVEL_WARNING` | `HEALTH_STATUS_FAIL` |
| Latest value outside caution limits only | `LIMIT_CAUTION:{channel}` | `ALERT_LEVEL_CAUTION` | `HEALTH_STATUS_WARN` |
| Channel drifting from its learned normal | `DRIFT:{channel}` | `ALERT_LEVEL_ADVISORY` | `HEALTH_STATUS_WARN` |
| Subsystem restart in the last 24 h | `RESTART:{subsystem}` | `ALERT_LEVEL_ADVISORY` | unchanged |
| No frames for `stale_hours` | `STALE` | `ALERT_LEVEL_CAUTION` | Downlink: `WARN` |
| No frames for 2 × `stale_hours` | `STALE` | `ALERT_LEVEL_WARNING` | Downlink: `FAIL` |
| Silent-satellite verdict | `SILENT` | `ALERT_LEVEL_WARNING` | Downlink: `FAIL`; roll-up `HEALTH_STATUS_OFFLINE` |
| Subsystem with data but no limits set | none | | `HEALTH_STATUS_NOT_READY`, message says the values aren't judged |

The roll-up is the worst component that is judged: a subsystem without limits shows `NOT_READY` on its own but doesn't make the whole satellite "not ready" when everything judged is fine. The roll-up is `NOT_READY` only when nothing is judged, and a silent satellite is always `HEALTH_STATUS_OFFLINE`. This keeps the console's rule that a channel without limits is never shown as nominal. Alerts leave `activeAlerts` when their condition clears, as Lattice requires.

## 5. Task: ExpeditePass (ICD-L2, ICD-L3)

### 5.1 Definition

Defined in `proto/downlinkdesk/v1/tasks.proto`. Over REST the specification arrives as JSON:

```json
{"@type": "type.googleapis.com/downlinkdesk.v1.ExpeditePass",
 "noradCatId": 60237, "observationId": 13421188, "deadlineMinutes": 180}
```

| Field | Required | Meaning |
|---|---|---|
| `noradCatId` | yes | A satellite this node tracks |
| `observationId` | no | A SatNOGS Network observation of it. If absent, the next one that hasn't ended |
| `deadlineMinutes` | no | How long after the pass ends to keep trying. Default `upload_grace_hours` (3 h), maximum 12 h |

### 5.2 Behaviour

Stations often upload a pass's frames minutes to hours after it ends, and Downlink Desk normally polls every 30 minutes. This task shortens that wait for one pass: after the pass ends, the node fetches that satellite every `expedite_minutes` (default 10, never under 5) until the station's frames arrive or the deadline passes.

### 5.3 Lifecycle

```
ExecuteRequest ─► MACHINE_RECEIPT ─► validate ─┬─► DONE_NOT_OK (ERROR_CODE_REJECTED)
                                               └─► WILCO ─► (pass ends) ─► EXECUTING ─┬─► DONE_OK            frames arrived
                                                                                       ├─► DONE_NOT_OK TIMEOUT  deadline passed
CancelRequest ─► DONE_NOT_OK (ERROR_CODE_CANCELLED)                                    └─► (expedited fetch every 10 min)
CompleteRequest ─► DONE_OK with the result so far
```

- **Rejected** when: the satellite isn't tracked; the observation doesn't exist or belongs to another satellite; its deadline has already passed; the same observation is already being expedited (the message names the existing task); or three tasks are already active.
- **Already heard**: if the station's frames are stored when the task arrives, it goes straight to `DONE_OK`.
- `STATUS_ACK` is not sent: the node accepts or rejects automatically, with no operator acknowledgement step at the edge.
- **Progress** (on `EXECUTING`) and **result** (on `DONE_OK`) are `ExpeditePassProgress` and `ExpeditePassResult` messages: fetch count, next fetch time, frames received and decoded, station, first and last frame time.
- **Status versions**: each update sends the task's current `statusVersion` plus one and adopts the version in the response. If Lattice rejects a version, the bridge reads the task (`GET`) and retries once with the server's version plus one.
- **Restarts**: task state is kept in `outbox.db`, so a restarted bridge resumes its tasks.

### 5.4 Budget towards SatNOGS

SatNOGS is volunteer-run, so expediting is bounded: at most 3 active tasks, at least 5 (default 10) minutes between expedited fetches of a satellite, and at most 12 hours per task. The worst case is 3 satellites × 6 fetches an hour, each a few requests 11 s apart, which is under the volume the 30-minute schedule plus history fill already uses on a new install. Expedited fetches go through the normal fetch job (same lock, same pacing), so they can never overlap the scheduled one.

## 6. Local-first behaviour and failure handling

### 6.1 Store and forward

Every publish goes through a queue in `outbox.db` first, then a sender drains it in order.

- **Coalescing.** Each snapshot is marked as a *transition* if its health status, alert codes or activity differ from the entity's previous snapshot. When a new snapshot is queued, unsent non-transition snapshots of the same entity are dropped. A day offline therefore leaves the latest state plus every alert change, not 2,880 position updates per satellite.
- **Replay order is safe.** Lattice applies an entity update only if its `provenance.sourceUpdateTime` is newer than what it holds, so replaying in time order lands every transition and finishes on the latest state, and a duplicate can't overwrite newer data.
- **Expiry is set at send time**, because Lattice requires a future `expiryTime`. A queued snapshot keeps its original `sourceUpdateTime`.
- **Bounds.** At most 200 transitions per entity and 5,000 rows in all; beyond that the oldest are dropped first, and the drop is counted in the node's Lattice link component.

### 6.2 Failure handling

| Failure | Behaviour |
|---|---|
| No network, DNS failure, timeout, connection reset | Stop draining; retry with backoff from 5 s doubling to 5 min, ±20% jitter. Keep queueing. |
| HTTP 429 or 5xx | Same backoff; honours `Retry-After`. |
| HTTP 401 | Refresh the OAuth token (client-credentials mode) and retry once; otherwise mark the link `AUTH_ERROR` and back off. |
| HTTP 400 on one item | The item is invalid, so retrying won't help: drop it, log it, count it, and carry on with the rest of the queue. |
| Task status version rejected | `GET` the task, retry once with the server's version (§5.3). |
| Long poll times out or returns nothing | Poll again immediately. |
| Bridge restarts | Queue and task state survive in `outbox.db`; the node ID and entity IDs are unchanged. |
| Pi clock not yet synced after boot | Publishing waits until the clock is past the newest stored data, the same rule the fetch job uses, so no entity is stamped in the past. |
| Console database busy | Reads retry; the bridge never takes a write lock on it. |

Lattice marks an asset's connection offline when it hasn't been updated for about a minute, so a 30-second publish interval gives Lattice an accurate picture of whether the node is reachable.

## 7. Rates and resource budget

| Item | Value |
|---|---|
| Payload sizes (measured, GRBBeta) | Satellite about 3.7 KB of JSON, node about 2.2 KB, each station about 1 KB |
| Traffic at 30 s with one satellite | About 17 MB a day upstream for the node and satellite, plus about 1 MB for stations |
| CPU on a Pi Zero 2 W | One SGP4 propagation and a few indexed queries per satellite per cycle; the heavier restart check is cached for 5 min |
| Memory | Standard library only; no SDK, pydantic or httpx on the Pi |
| Disk | `outbox.db` bounded at 5,000 rows (a few MB) |

## 8. Requirements and verification

T = test, A = analysis, I = inspection, D = demonstration. Tests are in `tests/test_lattice.py`.

| ID | Requirement | Method | Verified by |
|---|---|---|---|
| LAT-001 | Every entity and task status payload shall use only fields, types and enum values defined in the Lattice SDK's models. | T | Every test, through `assert_schema`, when the SDK is installed (`requirements-dev.txt`) |
| LAT-002 | Each tracked satellite shall be published with orbit, position, health and NORAD alias. | T | `test_entities_published` |
| LAT-003 | Satellite position and velocity shall agree with independent calculations to within 1 km and 10 m/s, and the orbit elements with SGP4's own TLE parser. | T | `test_position_and_velocity` (measured: under 1 mm and 0.03 m/s) |
| LAT-004 | Health and alerts shall follow the mapping in §4.5, including clearing an alert when its condition clears. | T | `test_health_mapping` |
| LAT-005 | Entity IDs shall be the same across bridge restarts. | T | `test_entities_published` |
| LAT-006 | When Lattice is unreachable, the bridge shall queue updates and deliver them in time order when it returns, keeping every alert transition. | T | `test_outage_and_replay` |
| LAT-007 | A Lattice outage shall not stop or slow the fetch job or the console. | T, I | `test_outage_and_replay` (fetch runs during the outage); separate process and database file |
| LAT-008 | An invalid item rejected with HTTP 400 shall not block the rest of the queue. | T | `test_outage_and_replay` |
| LAT-009 | ExpeditePass shall move through RECEIPT → WILCO → EXECUTING → DONE_OK when the pass's frames arrive. | T | `test_expedite_pass_done` |
| LAT-010 | ExpeditePass shall end DONE_NOT_OK with TIMEOUT at its deadline, REJECTED for an invalid request, and CANCELLED on a cancel request. | T | `test_expedite_pass_rejects_timeout_cancel` |
| LAT-011 | Expedited fetches of a satellite shall be at least `expedite_minutes` apart, with no more than 3 active tasks. | T | `test_expedite_pass_done` (spacing), `test_expedite_pass_rejects_timeout_cancel` (task limit) |
| LAT-012 | Task state shall survive a bridge restart. | T | `test_expedite_pass_done` |
| LAT-013 | The node's location shall be rounded to 0.1° unless configured otherwise. | T | `test_entities_published` |
| LAT-014 | Status updates rejected for a stale version shall be resent after reading the task's current version. | T | `test_expedite_pass_rejects_timeout_cancel` |

## 9. Design decisions

| Decision | Chosen | Alternatives considered | Why |
|---|---|---|---|
| Satellite template | Asset | Track | Lattice allows health only on assets, and health is what Downlink Desk adds. From the operations desk's point of view the spacecraft is the asset being monitored. Disposition stays neutral, since this node doesn't operate it. |
| Stations | Asset without health | Geo entity; omit | Geo entities are control measures (areas, lines), not equipment. Health would claim knowledge Downlink Desk doesn't have. |
| Protocol | REST | gRPC | Standard library only, so nothing extra to install on a Pi Zero; REST and gRPC carry the same data model. |
| Client | Standard library | Official SDK | The SDK brings httpx and pydantic. The tests use the SDK to validate payloads instead. |
| Queue location | Separate SQLite file | Table in the console's database | Isolates the bridge entirely: removing it leaves the console's data untouched, and its writes can't contend with the fetch job. |
| Task | Expedite a pass | Schedule a SatNOGS observation | Scheduling needs the operator's own station and SatNOGS Network rights, and would act on volunteer equipment. Expediting acts only on this node. |
| Node location | Rounded to 0.1° | Exact | The station is often someone's home. Exact location is one setting away. |

## 10. Open items

1. **Run against a Lattice Sandbox** once access is granted, and record the results here. Until then, verification is against the mock server (`tests/mock_lattice.py`) and the SDK's types.
2. **Register `downlinkdesk.v1` in the Buf Schema Registry**, so the task definition resolves in the Lattice UI.
3. **Confirm the status-version convention** (send current + 1) on a real deployment. The retry in §5.3 covers either reading.
4. **Confirm how the sandbox treats `connectionStatus`** for assets without a health component (the station entities).
5. **Location uncertainty.** Publish a covariance that grows with TLE age instead of stating the age only in the description.
