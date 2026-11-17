"""Contribution rebuilder: pointwise vs interval strategies, resumable.

Why two strategies (the brief asks that this trade-off be visible):

* pointwise — one ``event_attribution`` row per source event. A merge/split
  only rewrites the rows of the moved source identities: *local repair*.
* interval  — one aggregate row per entity per month. The aggregate stores no
  per-event delta, so a merge/split forces a *full recompute of every impacted
  month window*; a single wrong number cannot be patched in place.
* chain     — cross-entity behaviour chains are ordered sequences spanning
  entities; touching one entity invalidates the whole chain (largest radius).

Resilience
----------
* Every job works in small batches through a persisted cursor, so it can be
  interrupted at any batch boundary and resumed without redoing finished work.
* Batch queries are re-issued after every batch with the cursor only. New
  events ingested while the job runs show up in the next query automatically
  (high-water behaviour) — no restart, no missed events.
"""
from __future__ import annotations

import json

from .util import dumps, new_id, now_iso

BATCH = 25


class JobInterrupted(Exception):
    pass


def list_jobs(conn, limit=50):
    rows = conn.execute(
        "SELECT * FROM rebuild_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["impact"] = json.loads(r["impact_json"])
        del d["impact_json"]
        out.append(d)
    return out


def get_job(conn, job_id):
    r = conn.execute(
        "SELECT * FROM rebuild_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["impact"] = json.loads(r["impact_json"])
    del d["impact_json"]
    return d


def enqueue_seed_jobs(conn, version=1):
    ts = now_iso()
    for mode, scope in (
        ("pointwise", "local"),
        ("interval", "local-windows"),
        ("chain", "full"),
    ):
        conn.execute(
            "INSERT INTO rebuild_jobs"
            "(job_id,mode,status,impact_scope,impact_json,from_version,to_version,"
            "processed,total,created_at)"
            " VALUES(?,?, 'queued', ?, '{}', 0, ?, 0, 0, ?)",
            (new_id("job"), mode, scope, version, ts),
        )
    conn.commit()


def enqueue_catchup(conn, version=None):
    """New events arrived with no running job: enqueue catch-up work.

    Pointwise catches the unattributed rows directly; interval re-checks for
    window drift; chains are rebuilt because a new event may create or extend
    a cross-entity chain. No-ops when there is nothing pending.
    """
    pending = conn.execute(
        "SELECT COUNT(*) n FROM source_events se "
        "WHERE se.event_id NOT IN "
        "(SELECT event_id FROM event_attribution WHERE is_fresh=1)"
    ).fetchone()["n"]
    if not pending:
        return []
    from .adjudication import current_version

    version = version or current_version(conn)
    ts = now_iso()
    ids = []
    for mode, scope, est in (
        ("pointwise", "local", {"rows_to_rewrite": pending}),
        ("interval", "local-windows", {"windows_to_recompute": "drift"}),
        ("chain", "full", {"chains_to_recompute": "all"}),
    ):
        job_id = new_id("job")
        ids.append(job_id)
        conn.execute(
            "INSERT INTO rebuild_jobs"
            "(job_id,mode,status,impact_scope,impact_json,from_version,to_version,"
            "processed,total,created_at)"
            " VALUES(?,?,'queued',?, ?, ?, ?, 0, ?, ?)",
            (job_id, mode, scope, dumps(est), version, version, pending, ts),
        )
    conn.commit()
    return ids


def resume_job(conn, job_id):
    cur = conn.execute(
        "UPDATE rebuild_jobs SET status='queued', error=NULL "
        "WHERE job_id=? AND status='interrupted'",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def interrupt_job(conn, job_id):
    """Mark a running job for cooperative stop; the worker checks the flag."""
    cur = conn.execute(
        "UPDATE rebuild_jobs SET status='interrupting' "
        "WHERE job_id=? AND status IN ('queued','running')",
        (job_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def _stopped(conn, job_id):
    r = conn.execute(
        "SELECT status FROM rebuild_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    return r and r["status"] == "interrupting"


def _sweep_interrupting(conn):
    """An interrupt flag set before a job was claimed must still land the job
    in 'interrupted' (with whatever cursor it had) rather than stall forever.
    """
    cur = conn.execute(
        "UPDATE rebuild_jobs SET status='interrupted', finished_at=? "
        "WHERE status='interrupting'",
        (now_iso(),),
    )
    if cur.rowcount:
        conn.commit()


def _claim_next(conn):
    _sweep_interrupting(conn)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT * FROM rebuild_jobs WHERE status='queued' "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    if not row:
        conn.commit()
        return None
    conn.execute(
        "UPDATE rebuild_jobs SET status='running', started_at=? "
        "WHERE job_id=?",
        (now_iso(), row["job_id"]),
    )
    conn.commit()
    return dict(row)


def worker_tick(conn, stop_check=None):
    """Process one queued job to completion (or interruption). Returns job id."""
    job = _claim_next(conn)
    if not job:
        return None
    job_id = job["job_id"]
    try:
        if job["mode"] == "pointwise":
            _run_pointwise(conn, job_id, stop_check)
        elif job["mode"] == "interval":
            _run_interval(conn, job_id, stop_check)
        else:
            _run_chain(conn, job_id, stop_check)
        conn.execute(
            "UPDATE rebuild_jobs SET status='done', finished_at=? WHERE job_id=?",
            (now_iso(), job_id),
        )
        conn.commit()
    except JobInterrupted:
        conn.execute(
            "UPDATE rebuild_jobs SET status='interrupted', finished_at=? "
            "WHERE job_id=?",
            (now_iso(), job_id),
        )
        conn.commit()
    except Exception as exc:  # pragma: no cover - defensive
        conn.rollback()
        conn.execute(
            "UPDATE rebuild_jobs SET status='interrupted', error=?, "
            "finished_at=? WHERE job_id=?",
            (str(exc), now_iso(), job_id),
        )
        conn.commit()
    return job_id


# ------------------------------------------------------------------ pointwise
def _stale_event_batch(conn, cursor, limit):
    return conn.execute(
        """
        SELECT se.event_id, se.sid, em.eid
        FROM source_events se
        JOIN entity_membership em
          ON em.sid = se.sid AND em.valid_to IS NULL
        WHERE se.event_id > ?
          AND se.event_id NOT IN (
              SELECT event_id FROM event_attribution WHERE is_fresh=1
          )
        ORDER BY se.event_id LIMIT ?
        """,
        (cursor or "", limit),
    ).fetchall()


def _stale_event_count(conn):
    return conn.execute(
        """
        SELECT COUNT(*) n FROM source_events se
        WHERE se.event_id NOT IN (
            SELECT event_id FROM event_attribution WHERE is_fresh=1
        )
        """
    ).fetchone()["n"]


def _run_pointwise(conn, job_id, stop_check):
    job = get_job(conn, job_id)
    cursor = job["cursor_event"]
    while True:
        if _stopped(conn, job_id) or (stop_check and stop_check()):
            raise JobInterrupted()
        rows = _stale_event_batch(conn, cursor, BATCH)
        if not rows:
            break
        ts = now_iso()
        version = job["to_version"]
        conn.execute("BEGIN IMMEDIATE")  # one fsync per batch, not per row
        for r in rows:
            conn.execute(
                "INSERT INTO event_attribution"
                "(event_id,eid,sid,decided_version,is_fresh,updated_at)"
                " VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(event_id) DO UPDATE SET eid=excluded.eid,"
                " sid=excluded.sid, decided_version=excluded.decided_version,"
                " is_fresh=1, updated_at=excluded.updated_at",
                (r["event_id"], r["eid"], r["sid"], version, ts),
            )
            conn.execute(
                "INSERT INTO metric_freshness(scope,scope_key,valid_version,fresh)"
                " VALUES('pointwise',?,?,1) "
                "ON CONFLICT(scope,scope_key) DO UPDATE SET "
                "valid_version=excluded.valid_version, fresh=1",
                (r["event_id"], version),
            )
            cursor = r["event_id"]
        remaining = _stale_event_count(conn)
        conn.execute(
            "UPDATE rebuild_jobs SET cursor_event=?, processed=processed+?, "
            "total=? WHERE job_id=?",
            (cursor, len(rows), len(rows) + remaining, job_id),
        )
        conn.commit()


# ------------------------------------------------------------------- interval
def _window_batch(conn, cursor, limit):
    """Windows containing any event whose snapshot is not fresh.

    A window is recomputed *for every entity present in it* — the aggregate has
    no per-event delta to patch, which is exactly the 'cannot locally repair'
    property demonstrated in the UI cost preview.
    """
    return conn.execute(
        """
        SELECT DISTINCT substr(se.occurred_at,1,7) AS w
        FROM source_events se
        WHERE substr(se.occurred_at,1,7) > ?
          AND se.event_id NOT IN (
              SELECT ise.event_id FROM interval_snapshot_events ise
              JOIN interval_snapshots s ON s.snap_id = ise.snap_id
              WHERE s.is_fresh=1
          )
        ORDER BY w LIMIT ?
        """,
        (cursor or "", limit),
    ).fetchall()


def _drift_windows(conn):
    """Full scan for drift: new events that landed in an already-passed window
    while the job was running. Independent of the resume cursor."""
    return conn.execute(
        """
        SELECT DISTINCT substr(se.occurred_at,1,7) AS w
        FROM source_events se
        WHERE se.event_id NOT IN (
            SELECT ise.event_id FROM interval_snapshot_events ise
            JOIN interval_snapshots s ON s.snap_id = ise.snap_id
            WHERE s.is_fresh=1
        )
        """
    ).fetchall()


def _rebuild_window(conn, w, version):
    """Recompute *all* entity aggregates inside one month window."""
    window_start = f"{w}-01"
    agg = conn.execute(
        """
        SELECT em.eid, COUNT(*) n, COALESCE(SUM(se.amount),0) amt,
               GROUP_CONCAT(se.event_id) ids
        FROM source_events se
        JOIN entity_membership em
          ON em.sid=se.sid AND em.valid_to IS NULL
        WHERE substr(se.occurred_at,1,7)=?
        GROUP BY em.eid
        """,
        (w,),
    ).fetchall()
    # Remove previous (possibly stale) snapshots for this window.
    old = conn.execute(
        "SELECT snap_id FROM interval_snapshots WHERE window_start=?",
        (window_start,),
    ).fetchall()
    for o in old:
        conn.execute(
            "DELETE FROM interval_snapshot_events WHERE snap_id=?",
            (o["snap_id"],),
        )
    conn.execute(
        "DELETE FROM interval_snapshots WHERE window_start=?", (window_start,)
    )
    ts = now_iso()
    for a in agg:
        snap_id = new_id("snap")
        conn.execute(
            "INSERT INTO interval_snapshots"
            "(snap_id,eid,window_start,window_end,event_count,"
            "total_amount,built_version,is_fresh,updated_at)"
            " VALUES(?,?,?,DATE(?, '+1 month'),?,?,?,1,?)",
            (
                snap_id,
                a["eid"],
                window_start,
                window_start,
                a["n"],
                a["amt"],
                version,
                ts,
            ),
        )
        for ev_id in (a["ids"] or "").split(","):
            if ev_id:
                conn.execute(
                    "INSERT OR IGNORE INTO interval_snapshot_events"
                    "(snap_id,event_id) VALUES(?,?)",
                    (snap_id, ev_id),
                )
        conn.execute(
            "INSERT INTO metric_freshness"
            "(scope,scope_key,valid_version,fresh)"
            " VALUES('interval',?,?,1) "
            "ON CONFLICT(scope,scope_key) DO UPDATE SET "
            "valid_version=excluded.valid_version, fresh=1",
            (snap_id, version),
        )


def _run_interval(conn, job_id, stop_check):
    job = get_job(conn, job_id)
    cursor = job["cursor_event"]
    version = job["to_version"]
    done_windows = set()
    while True:
        if _stopped(conn, job_id) or (stop_check and stop_check()):
            raise JobInterrupted()
        windows = _window_batch(conn, cursor, max(1, BATCH // 5))
        if not windows:
            break
        conn.execute("BEGIN IMMEDIATE")
        for wr in windows:
            w = wr["w"]
            _rebuild_window(conn, w, version)
            done_windows.add(w)
            cursor = w
        conn.execute(
            "UPDATE rebuild_jobs SET cursor_event=?, processed=processed+? "
            "WHERE job_id=?",
            (cursor, len(windows), job_id),
        )
        conn.commit()
    # Drift finalization (cursor-independent): pick up events that arrived in
    # a window the cursor had already passed. On interruption this is skipped
    # and re-runs resume + re-check on the next tick.
    if _stopped(conn, job_id) or (stop_check and stop_check()):
        raise JobInterrupted()
    extra = [r["w"] for r in _drift_windows(conn)]
    if extra:
        conn.execute("BEGIN IMMEDIATE")
        for w in extra:
            if _stopped(conn, job_id) or (stop_check and stop_check()):
                conn.rollback()
                raise JobInterrupted()
            _rebuild_window(conn, w, version)
            done_windows.add(w)
        conn.execute(
            "UPDATE rebuild_jobs SET processed=processed+? WHERE job_id=?",
            (len(extra), job_id),
        )
        conn.commit()


# ---------------------------------------------------------------------- chain
def _chain_keys_batch(conn, cursor, limit):
    """Device keys shared by events of >=2 distinct entities (cross-entity)."""
    return conn.execute(
        """
        SELECT json_extract(payload_json,'$.device_id') AS k
        FROM source_events
        WHERE json_extract(payload_json,'$.device_id') IS NOT NULL
          AND json_extract(payload_json,'$.device_id') > ?
        GROUP BY k
        HAVING COUNT(DISTINCT sid) >= 1
        ORDER BY k LIMIT ?
        """,
        (cursor or "", limit),
    ).fetchall()


def _run_chain(conn, job_id, stop_check):
    job = get_job(conn, job_id)
    cursor = job["cursor_event"]
    version = job["to_version"]
    rebuilt = set()
    while True:
        if _stopped(conn, job_id) or (stop_check and stop_check()):
            raise JobInterrupted()
        keys = _chain_keys_batch(conn, cursor, BATCH)
        if not keys:
            break
        ts = now_iso()
        conn.execute("BEGIN IMMEDIATE")
        for kr in keys:
            key = kr["k"]
            events = conn.execute(
                """
                SELECT se.event_id, se.occurred_at, em.eid
                FROM source_events se
                JOIN entity_membership em
                  ON em.sid=se.sid AND em.valid_to IS NULL
                WHERE json_extract(se.payload_json,'$.device_id')=?
                ORDER BY se.occurred_at, se.event_id
                """,
                (key,),
            ).fetchall()
            eids = sorted({e["eid"] for e in events})
            if len(eids) >= 2:  # genuine cross-entity chain
                seq = [
                    {"event_id": e["event_id"], "eid": e["eid"],
                     "occurred_at": e["occurred_at"]}
                    for e in events
                ]
                conn.execute(
                    "INSERT INTO behavior_chains"
                    "(chain_id,chain_key,seq_json,touches_json,built_version,"
                    "is_fresh,updated_at)"
                    " VALUES(?,?,?,?,?,1,?) "
                    "ON CONFLICT(chain_key) DO UPDATE SET seq_json=excluded.seq_json,"
                    " touches_json=excluded.touches_json,"
                    " built_version=excluded.built_version, is_fresh=1,"
                    " updated_at=excluded.updated_at",
                    (
                        new_id("chain"),
                        key,
                        dumps(seq),
                        dumps(eids),
                        version,
                        ts,
                    ),
                )
            else:
                # A merge can collapse a former cross-entity chain into a
                # single-entity sequence. That chain no longer exists as a
                # *cross-entity* chain; remove it so it can't linger as stale.
                conn.execute(
                    "DELETE FROM behavior_chains WHERE chain_key=?", (key,)
                )
            rebuilt.add(key)
            cursor = key
        conn.execute(
            "UPDATE rebuild_jobs SET cursor_event=?, processed=processed+? "
            "WHERE job_id=?",
            (cursor, len(keys), job_id),
        )
        conn.commit()
    # Defensive sweep for chains whose key disappeared from the event stream
    # entirely; runs only when the job actually finished, never after interrupt.
    if rebuilt:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" * len(rebuilt))
        stale = conn.execute(
            f"SELECT chain_id FROM behavior_chains WHERE chain_key NOT IN "
            f"({placeholders})",
            tuple(rebuilt),
        ).fetchall()
        for s in stale:
            conn.execute(
                "DELETE FROM behavior_chains WHERE chain_id=?", (s["chain_id"],)
            )
        conn.commit()
