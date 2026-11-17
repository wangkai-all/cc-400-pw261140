"""Reporting core.

Every number returned here carries provenance:

* ``built_version``  — the adjudication version whose mapping produced it;
* pointwise rows name the exact source events behind a count/amount;
* interval snapshots name their provenance rows in
  ``interval_snapshot_events`` — an aggregate is never an untraceable black box;
* stale rows are counted separately and flagged, never silently presented as
  current truth.
"""
from __future__ import annotations

import json

from .adjudication import current_version
from .util import dumps, new_id, now_iso


def freshness_dashboard(conn):
    def q(scope):
        row = conn.execute(
            "SELECT COUNT(*) total, COALESCE(SUM(is_fresh),0) fresh "
            "FROM "
            + {
                "pointwise": "event_attribution",
                "interval": "interval_snapshots",
                "chain": "behavior_chains",
            }[scope]
        ).fetchone()
        return {"total": row["total"], "fresh": row["fresh"],
                "stale": row["total"] - row["fresh"]}

    pending_events = conn.execute(
        "SELECT COUNT(*) n FROM source_events se "
        "WHERE se.event_id NOT IN "
        "(SELECT event_id FROM event_attribution WHERE is_fresh=1)"
    ).fetchone()["n"]
    return {
        "adjudication_version": current_version(conn),
        "pending_events": pending_events,
        "pointwise": q("pointwise"),
        "interval": q("interval"),
        "chain": q("chain"),
    }


def build_entity_report(conn):
    version = current_version(conn)
    ts = now_iso()
    entities = []
    rows = conn.execute(
        """
        SELECT ea.eid,
               COUNT(*)                                       AS event_count,
               COALESCE(SUM(se.amount),0)                     AS total_amount,
               SUM(ea.is_fresh)                               AS fresh_rows,
               GROUP_CONCAT(se.event_id)                      AS event_ids,
               MAX(ea.decided_version)                        AS max_decided
        FROM event_attribution ea
        JOIN source_events se ON se.event_id = ea.event_id
        GROUP BY ea.eid ORDER BY ea.eid
        """
    ).fetchall()
    for r in rows:
        event_ids = (r["event_ids"] or "").split(",")
        decided_versions = [
            x["v"]
            for x in conn.execute(
                "SELECT DISTINCT decided_version v FROM event_attribution "
                "WHERE eid=? ORDER BY 1",
                (r["eid"],),
            )
        ]
        entities.append(
            {
                "eid": r["eid"],
                "event_count": r["event_count"],
                "total_amount": round(r["total_amount"], 2),
                "fresh_rows": r["fresh_rows"],
                "stale_rows": r["event_count"] - r["fresh_rows"],
                "decided_versions": decided_versions,
                "provenance": {
                    "strategy": "pointwise",
                    "source_event_ids": event_ids,
                    "adjudication_version": r["max_decided"],
                },
            }
        )

    # Interval view of the same entities (separate storage strategy).
    snaps = conn.execute(
        """
        SELECT s.*, GROUP_CONCAT(ise.event_id) ids
        FROM interval_snapshots s
        LEFT JOIN interval_snapshot_events ise ON ise.snap_id=s.snap_id
        GROUP BY s.snap_id ORDER BY s.eid, s.window_start
        """
    ).fetchall()
    interval_view = [
        {
            "snap_id": s["snap_id"],
            "eid": s["eid"],
            "window": s["window_start"],
            "event_count": s["event_count"],
            "total_amount": round(s["total_amount"], 2),
            "is_fresh": bool(s["is_fresh"]),
            "built_version": s["built_version"],
            "source_event_ids": [x for x in (s["ids"] or "").split(",") if x],
        }
        for s in snaps
    ]

    chains = [
        {
            "chain_id": r["chain_id"],
            "chain_key": r["chain_key"],
            "touches": json.loads(r["touches_json"]),
            "built_version": r["built_version"],
            "is_fresh": bool(r["is_fresh"]),
            "length": len(json.loads(r["seq_json"])),
        }
        for r in conn.execute(
            "SELECT * FROM behavior_chains ORDER BY chain_key"
        ).fetchall()
    ]

    body = {
        "data_kind": "synthetic",
        "disclaimer": "全部身份与事件均为程序生成的虚构数据，系统不包含真实个人识别能力。",
        "adjudication_version": version,
        "freshness": freshness_dashboard(conn),
        "entities": entities,
        "interval_view": interval_view,
        "behavior_chains": chains,
    }
    report_id = new_id("rpt")
    conn.execute(
        "INSERT INTO report_records"
        "(report_id,report_type,params_json,body_json,built_version,created_at)"
        " VALUES(?,?, '{}', ?, ?, ?)",
        (report_id, "entity_summary", dumps(body), version, ts),
    )
    conn.commit()
    return report_id, body


def list_reports(conn):
    return [
        {
            "report_id": r["report_id"],
            "report_type": r["report_type"],
            "built_version": r["built_version"],
            "created_at": r["created_at"],
        }
        for r in conn.execute(
            "SELECT report_id,report_type,built_version,created_at "
            "FROM report_records ORDER BY created_at DESC LIMIT 50"
        )
    ]


def get_report(conn, report_id):
    r = conn.execute(
        "SELECT * FROM report_records WHERE report_id=?", (report_id,)
    ).fetchone()
    if not r:
        return None
    out = dict(r)
    out["body"] = json.loads(r["body_json"])
    del out["body_json"]
    return out


def trace_event(conn, event_id):
    """Full drill-down: source event -> mapping timeline -> attribution ->
    snapshot -> chain -> report versions."""
    ev = conn.execute(
        "SELECT * FROM source_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if not ev:
        return None
    sid = ev["sid"]
    history = [
        dict(r)
        for r in conn.execute(
            """SELECT em.eid, em.valid_from, em.valid_to, v.action, v.rationale,
                      v.decided_by
               FROM entity_membership em
               LEFT JOIN adjudication_versions v ON v.version=em.valid_from
               WHERE em.sid=? ORDER BY em.valid_from""",
            (sid,),
        )
    ]
    attr = conn.execute(
        "SELECT * FROM event_attribution WHERE event_id=?", (event_id,)
    ).fetchone()
    snaps = [
        dict(r)
        for r in conn.execute(
            "SELECT s.snap_id,s.eid,s.window_start,s.built_version,s.is_fresh "
            "FROM interval_snapshot_events ise "
            "JOIN interval_snapshots s ON s.snap_id=ise.snap_id "
            "WHERE ise.event_id=?",
            (event_id,),
        )
    ]
    chains = []
    for r in conn.execute("SELECT * FROM behavior_chains"):
        seq = json.loads(r["seq_json"])
        if any(s["event_id"] == event_id for s in seq):
            chains.append(
                {"chain_id": r["chain_id"], "chain_key": r["chain_key"],
                 "built_version": r["built_version"], "is_fresh": bool(r["is_fresh"])}
            )
    return {
        "source_event": dict(ev),
        "membership_timeline": history,
        "attribution": dict(attr) if attr else None,
        "interval_snapshots": snaps,
        "behavior_chains": chains,
    }
