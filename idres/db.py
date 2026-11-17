"""SQLite schema (WAL, single shared database across three independent processes).

Design notes
------------
* Every adjudication change is an *immutable version*. Mapping rows carry the
  version that created them; rows are never mutated in place, they are closed
  (valid_to) and a new row opened.
* ``adjudication_versions`` has a monotonic version counter guarded by a
  compare-and-set in the adjudication service so a late-arriving old decision
  can never overwrite a newer conclusion.
* Pointwise attribution (one row per event) and interval snapshots (one row
  per entity per month) coexist so the UI can show both storage strategies and
  their very different repair costs.
"""

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- source data
CREATE TABLE IF NOT EXISTS source_identities (
    sid           TEXT PRIMARY KEY,           -- source identity id
    source_system TEXT NOT NULL,
    external_ref  TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    traits_json   TEXT NOT NULL,              -- raw observed traits
    created_at    TEXT NOT NULL,
    UNIQUE(source_system, external_ref)
);

CREATE TABLE IF NOT EXISTS source_events (
    event_id      TEXT PRIMARY KEY,           -- zero padded? ordered by event_id
    sid           TEXT NOT NULL REFERENCES source_identities(sid),
    event_type    TEXT NOT NULL,
    amount        REAL,
    occurred_at   TEXT NOT NULL,              -- event time
    ingested_at   TEXT NOT NULL,              -- ingestion time
    payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_sid ON source_events(sid);
CREATE INDEX IF NOT EXISTS idx_events_time ON source_events(occurred_at);

CREATE TABLE IF NOT EXISTS candidate_evidence (
    evidence_id   TEXT PRIMARY KEY,
    sid_a         TEXT NOT NULL REFERENCES source_identities(sid),
    sid_b         TEXT NOT NULL REFERENCES source_identities(sid),
    evidence_type TEXT NOT NULL,              -- same_email / same_device / name_geo / contradicting_email ...
    score         REAL NOT NULL,
    detail_json   TEXT NOT NULL,
    found_at      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',  -- open | accepted | rejected
    decided_by_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ev_pair ON candidate_evidence(sid_a, sid_b);

-- ------------------------------------------------------------- adjudication
CREATE TABLE IF NOT EXISTS adjudication_versions (
    version        INTEGER PRIMARY KEY,
    action         TEXT NOT NULL,             -- seed | merge | split
    decided_by     TEXT NOT NULL,             -- user / auto / simulated_late
    base_version   INTEGER,                   -- optimistic-lock base
    rationale      TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- Entity-mapping timeline. Each row says sid belongs to eid for
-- [valid_from, valid_to). valid_to IS NULL = current.
CREATE TABLE IF NOT EXISTS entity_membership (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sid          TEXT NOT NULL,
    eid          TEXT NOT NULL,               -- analysis entity
    valid_from   INTEGER NOT NULL,
    valid_to     INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_em_sid ON entity_membership(sid);
CREATE INDEX IF NOT EXISTS idx_em_eid ON entity_membership(eid);

CREATE TABLE IF NOT EXISTS analysis_entities (
    eid          TEXT PRIMARY KEY,
    created_version INTEGER NOT NULL,
    superseded   INTEGER NOT NULL DEFAULT 0,  -- 1 when all its sids were moved away
    note         TEXT
);

-- Rejected (stale) decisions are kept verbatim for audit instead of being
-- silently dropped.
CREATE TABLE IF NOT EXISTS stale_decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_version INTEGER NOT NULL,
    current_version   INTEGER NOT NULL,
    payload_json   TEXT NOT NULL,
    reason         TEXT NOT NULL,
    received_at    TEXT NOT NULL
);

-- ------------------------------------------------------------------ rebuild
CREATE TABLE IF NOT EXISTS rebuild_jobs (
    job_id        TEXT PRIMARY KEY,
    mode          TEXT NOT NULL,              -- pointwise | interval | chain
    status        TEXT NOT NULL,              -- queued | running | interrupted | done
    impact_scope  TEXT NOT NULL,              -- local | full
    impact_json   TEXT NOT NULL,              -- estimated rows/windows cost
    from_version  INTEGER NOT NULL,
    to_version    INTEGER NOT NULL,
    cursor_event  TEXT,                       -- resume cursor into source_events
    processed     INTEGER NOT NULL DEFAULT 0,
    total         INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

-- Pointwise: per-event attribution under a specific adjudication version.
CREATE TABLE IF NOT EXISTS event_attribution (
    event_id        TEXT PRIMARY KEY,
    eid             TEXT NOT NULL,
    sid             TEXT NOT NULL,
    decided_version INTEGER NOT NULL,
    is_fresh        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ea_eid ON event_attribution(eid);
CREATE INDEX IF NOT EXISTS idx_ea_fresh ON event_attribution(is_fresh);

-- Interval: one aggregate per (entity, month window). Deliberately no event
-- detail inside the aggregate; the provenance table restores the link.
CREATE TABLE IF NOT EXISTS interval_snapshots (
    snap_id         TEXT PRIMARY KEY,
    eid             TEXT NOT NULL,
    window_start    TEXT NOT NULL,            -- YYYY-MM-01
    window_end      TEXT NOT NULL,            -- exclusive
    event_count     INTEGER NOT NULL,
    total_amount    REAL NOT NULL,
    built_version   INTEGER NOT NULL,
    is_fresh        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_is_eid ON interval_snapshots(eid);

-- Interval provenance: snapshots remain traceable back to individual source
-- events (requirement: reports trace to source events).
CREATE TABLE IF NOT EXISTS interval_snapshot_events (
    snap_id  TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (snap_id, event_id)
);

-- Cross-entity behaviour chains (e.g. device shared across entities ->
-- transfer-like sequences). Larger blast radius than counts/amounts.
CREATE TABLE IF NOT EXISTS behavior_chains (
    chain_id        TEXT PRIMARY KEY,
    chain_key       TEXT NOT NULL UNIQUE,     -- e.g. device id (conflict target)
    seq_json        TEXT NOT NULL,            -- ordered [event_id, eid, ...]
    touches_json    TEXT NOT NULL,            -- set of eids touched
    built_version   INTEGER NOT NULL,
    is_fresh        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bc_version ON behavior_chains(built_version);

-- Freshness bookkeeping: which derived rows were obsoleted by which version.
CREATE TABLE IF NOT EXISTS metric_freshness (
    scope        TEXT NOT NULL,               -- pointwise | interval | chain
    scope_key    TEXT NOT NULL,               -- event_id / snap_id / chain_id
    valid_version INTEGER NOT NULL,           -- latest mapping version the row reflects
    fresh        INTEGER NOT NULL,
    PRIMARY KEY (scope, scope_key)
);

-- ------------------------------------------------------------------ reports
CREATE TABLE IF NOT EXISTS report_records (
    report_id      TEXT PRIMARY KEY,
    report_type    TEXT NOT NULL,
    params_json    TEXT NOT NULL,
    body_json      TEXT NOT NULL,
    built_version  INTEGER NOT NULL,
    built_job_id   TEXT,
    created_at     TEXT NOT NULL
);
"""


def connect(db_path: str):
    import sqlite3

    # isolation_level=None -> autocommit; every transaction boundary in the
    # codebase is explicit (BEGIN IMMEDIATE / commit / rollback), which is what
    # the compare-and-set and job-claim logic rely on.
    conn = sqlite3.connect(
        db_path, timeout=15, check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(db_path: str):
    import os

    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('data_kind','synthetic')"
        )
    finally:
        conn.close()
