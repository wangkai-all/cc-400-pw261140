"""Adjudication core: versioned merge/split with compare-and-set.

All rules live here so the HTTP service is a thin wrapper and the same logic is
exercised directly by unit tests.

Guarantees
----------
* Decisions are immutable versions. A merge/split writes a new
  ``adjudication_versions`` row; mapping rows are closed+reopened, never
  overwritten, so every historical membership is reconstructable.
* A decision carrying ``base_version`` older than the current version is
  rejected (compare-and-set) and recorded in ``stale_decisions``. A late
  worker therefore can never silently revert a newer conclusion.
* Every accepted decision *also* obsoletes the derived metrics it touches
  (pointwise rows, interval snapshots, behaviour chains) and enqueues rebuild
  work. It is impossible to change the mapping while leaving a stale
  dedup metric looking fresh — the failure mode called out in the brief.
"""
from __future__ import annotations

from .util import dumps, loads, new_id, now_iso


class StaleDecision(Exception):
    def __init__(self, attempted: int, current: int):
        self.attempted = attempted
        self.current = current
        super().__init__(
            f"base_version={attempted} is stale; current_version={current}"
        )


def current_version(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM adjudication_versions"
    ).fetchone()
    return int(row["v"])


def current_mapping(conn) -> dict[str, str]:
    """sid -> eid for currently-open memberships."""
    rows = conn.execute(
        "SELECT sid, eid FROM entity_membership WHERE valid_to IS NULL"
    ).fetchall()
    return {r["sid"]: r["eid"] for r in rows}


# ----------------------------------------------------------------- read models
def list_entities(conn):
    entities = {
        r["eid"]: {"eid": r["eid"], "note": r["note"], "sids": []}
        for r in conn.execute("SELECT * FROM analysis_entities WHERE superseded=0")
    }
    for r in conn.execute(
        """SELECT em.sid, em.eid, si.display_name, si.source_system
           FROM entity_membership em
           JOIN source_identities si ON si.sid = em.sid
           WHERE em.valid_to IS NULL ORDER BY em.eid, em.sid"""
    ):
        if r["eid"] in entities:
            entities[r["eid"]]["sids"].append(
                {
                    "sid": r["sid"],
                    "display_name": r["display_name"],
                    "source_system": r["source_system"],
                }
            )
    counts = {
        r["eid"]: {"events": r["n"], "fresh": r["fresh"]}
        for r in conn.execute(
            "SELECT eid, COUNT(*) n, SUM(is_fresh) fresh "
            "FROM event_attribution GROUP BY eid"
        )
    }
    out = []
    for e in entities.values():
        c = counts.get(e["eid"], {"events": 0, "fresh": 0})
        e["event_count"] = c["events"] or 0
        e["fresh_count"] = c["fresh"] or 0
        out.append(e)
    return sorted(out, key=lambda e: e["eid"])


def list_evidence(conn, status: str | None = None):
    sql = (
        "SELECT * FROM candidate_evidence "
        + ("WHERE status=? " if status else "")
        + "ORDER BY score DESC, found_at"
    )
    params = (status,) if status else ()
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["detail"] = loads(r["detail_json"], {})
        del d["detail_json"]
        out.append(d)
    return out


def membership_history(conn, sid: str):
    rows = conn.execute(
        """SELECT em.*, v.action, v.rationale, v.decided_by
           FROM entity_membership em
           LEFT JOIN adjudication_versions v ON v.version = em.valid_from
           WHERE em.sid=? ORDER BY em.valid_from""",
        (sid,),
    ).fetchall()
    return [dict(r) for r in rows]


def stale_decisions(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stale_decisions ORDER BY id DESC LIMIT 50"
    )]


# ------------------------------------------------------------- impact analysis
def _events_for_sids(conn, sids):
    if not sids:
        return []
    q = ",".join("?" * len(sids))
    return [
        r["event_id"]
        for r in conn.execute(
            f"SELECT event_id FROM source_events WHERE sid IN ({q})", sids
        )
    ]


def _windows_for_sids(conn, sids):
    if not sids:
        return set()
    q = ",".join("?" * len(sids))
    rows = conn.execute(
        f"SELECT DISTINCT substr(occurred_at,1,7) AS w "
        f"FROM source_events WHERE sid IN ({q})",
        sids,
    ).fetchall()
    return {r["w"] for r in rows}


def _chains_for_eids(conn, eids):
    if not eids:
        return []
    out = []
    for r in conn.execute("SELECT chain_id, touches_json FROM behavior_chains"):
        touches = set(loads(r["touches_json"], []))
        if touches & set(eids):
            out.append(r["chain_id"])
    return out


def impact_preview(conn, sids, eids):
    """Cost comparison shown *before* the user confirms a merge/split.

    pointwise = local repair: only the events of the moved sids are rewritten.
    interval  = the stored aggregate holds no per-event delta, so every month
                window those entities appear in must be fully recomputed.
    chain     = cross-entity chains are sequences; touching one entity can
                change any chain that references it -> largest blast radius.
    """
    event_ids = _events_for_sids(conn, sids)
    windows = sorted(_windows_for_sids(conn, sids))
    chain_ids = _chains_for_eids(conn, eids)
    total_events = conn.execute(
        "SELECT COUNT(*) n FROM source_events"
    ).fetchone()["n"]
    total_windows = conn.execute(
        "SELECT COUNT(DISTINCT substr(occurred_at,1,7)) n FROM source_events"
    ).fetchone()["n"]
    return {
        "pointwise": {
            "scope": "local",
            "rows_to_rewrite": len(event_ids),
            "repairable_locally": True,
            "note": "逐事件归属：只重写被移动来源身份的事件行，可局部修复。",
        },
        "interval": {
            "scope": "local-windows/full-aggregate",
            "windows_to_recompute": len(windows),
            "windows_total": total_windows,
            "repairable_locally": False,
            "note": "区间快照不保存逐事件增量，涉及窗口必须整窗重建，无法只改一个数。",
        },
        "chain": {
            "scope": "full",
            "chains_to_recompute": len(chain_ids),
            "repairable_locally": False,
            "note": "跨实体行为链是有序序列，触一链即需整链重算，影响范围最大。",
        },
        "total_events": total_events,
        "affected_event_ids": event_ids,
    }


# --------------------------------------------------------------- the CAS write
def commit_decision(
    conn,
    action: str,
    base_version: int,
    decided_by: str,
    rationale: str,
    merge_groups=None,
    split_specs=None,
):
    """Apply a merge or split decision under optimistic concurrency control.

    merge_groups: [[sid, sid, ...], ...]  -> all sids in a group map to one eid
    split_specs : [{new_eid?, sids:[...]}] -> sids move onto brand-new entities

    Returns dict(accepted, version, ...). On staleness the decision is refused
    and an audit row is written in a separate transaction (the business
    transaction itself is rolled back).
    """
    assert action in ("merge", "split")
    merge_groups = merge_groups or []
    split_specs = split_specs or []

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = current_version(conn)
        if int(base_version) != cur:
            raise StaleDecision(int(base_version), cur)

        new_version = cur + 1
        ts = now_iso()
        conn.execute(
            "INSERT INTO adjudication_versions"
            "(version,action,decided_by,base_version,rationale,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (new_version, action, decided_by, cur, rationale, ts),
        )

        mapping = current_mapping(conn)
        moved_sids = []
        impacted_eids = set()

        def move(sid, target_eid):
            old = mapping.get(sid)
            if old is None:
                raise ValueError(f"unknown sid {sid}")
            impacted_eids.add(old)
            if old != target_eid:
                conn.execute(
                    "UPDATE entity_membership SET valid_to=? "
                    "WHERE sid=? AND valid_to IS NULL",
                    (new_version, sid),
                )
                conn.execute(
                    "INSERT INTO entity_membership"
                    "(sid,eid,valid_from,valid_to,created_at)"
                    " VALUES(?,?,?,NULL,?)",
                    (sid, target_eid, new_version, ts),
                )
                mapping[sid] = target_eid
                moved_sids.append(sid)

        if action == "merge":
            for group in merge_groups:
                group = list(group)
                if len(group) < 2:
                    raise ValueError("merge group needs >= 2 sids")
                target = mapping[group[0]]
                for sid in group:
                    move(sid, target)
                conn.execute(
                    "UPDATE candidate_evidence SET status='accepted',"
                    "decided_by_version=? "
                    "WHERE sid_a IN (%s) AND sid_b IN (%s) AND status='open'"
                    % (",".join("?" * len(group)), ",".join("?" * len(group))),
                    (new_version, *group, *group),
                )
        else:
            for spec in split_specs:
                sids = spec["sids"]
                new_eid = spec.get("new_eid") or new_id("ent")
                conn.execute(
                    "INSERT INTO analysis_entities"
                    "(eid,created_version,note) VALUES(?,?,?)",
                    (new_eid, new_version, spec.get("note", "split entity")),
                )
                for sid in sids:
                    move(sid, new_eid)
                    impacted_eids.add(new_eid)

        # Supersede entities left with no open membership.
        for eid in impacted_eids:
            n = conn.execute(
                "SELECT COUNT(*) n FROM entity_membership "
                "WHERE eid=? AND valid_to IS NULL",
                (eid,),
            ).fetchone()["n"]
            if n == 0:
                conn.execute(
                    "UPDATE analysis_entities SET superseded=1 WHERE eid=?",
                    (eid,),
                )

        # ---- Obsolete derived metrics + enqueue rebuild work ---------------
        impact = _obsolete_and_enqueue(
            conn, new_version, moved_sids, impacted_eids, ts
        )
        conn.commit()
        return {
            "accepted": True,
            "version": new_version,
            "action": action,
            "moved_sids": moved_sids,
            "impact": impact,
        }
    except StaleDecision as exc:
        conn.rollback()
        # Separate transaction: refusal must itself be durably auditable.
        conn.execute(
            "INSERT INTO stale_decisions"
            "(attempted_version,current_version,payload_json,reason,received_at)"
            " VALUES(?,?,?,?,?)",
            (
                exc.attempted,
                exc.current,
                dumps(
                    {
                        "action": action,
                        "merge_groups": merge_groups,
                        "split_specs": split_specs,
                        "decided_by": decided_by,
                    }
                ),
                str(exc),
                now_iso(),
            ),
        )
        conn.commit()
        return {
            "accepted": False,
            "reason": str(exc),
            "attempted_version": exc.attempted,
            "current_version": exc.current,
        }
    except Exception:
        conn.rollback()
        raise


def _obsolete_and_enqueue(conn, version, moved_sids, impacted_eids, ts):
    event_ids = _events_for_sids(conn, moved_sids)
    windows = sorted(_windows_for_sids(conn, moved_sids))
    chain_ids = _chains_for_eids(conn, impacted_eids)

    if event_ids:
        q = ",".join("?" * len(event_ids))
        conn.execute(
            f"UPDATE event_attribution SET is_fresh=0 "
            f"WHERE event_id IN ({q})",
            event_ids,
        )
        conn.executemany(
            "INSERT INTO metric_freshness(scope,scope_key,valid_version,fresh)"
            " VALUES('pointwise',?,? ,0) "
            "ON CONFLICT(scope,scope_key) DO UPDATE SET "
            "valid_version=excluded.valid_version, fresh=0",
            [(eid_, version) for eid_ in event_ids],
        )
    if windows:
        q = ",".join("?" * len(windows))
        conn.execute(
            f"UPDATE interval_snapshots SET is_fresh=0 "
            f"WHERE substr(window_start,1,7) IN ({q}) "
            f"AND eid IN (SELECT eid FROM entity_membership WHERE valid_to IS NULL)",
            windows,
        )
    if chain_ids:
        q = ",".join("?" * len(chain_ids))
        conn.execute(
            f"UPDATE behavior_chains SET is_fresh=0 WHERE chain_id IN ({q})",
            chain_ids,
        )

    impact = impact_preview(conn, moved_sids, list(impacted_eids))

    jobs = [
        ("pointwise", "local", {"rows_to_rewrite": len(event_ids)}),
        ("interval", "local-windows", {"windows_to_recompute": len(windows)}),
        ("chain", "full", {"chains_to_recompute": len(chain_ids)}),
    ]
    for mode, scope, est in jobs:
        conn.execute(
            "INSERT INTO rebuild_jobs"
            "(job_id,mode,status,impact_scope,impact_json,from_version,to_version,"
            "processed,total,created_at)"
            " VALUES(?,?, 'queued', ?, ?, ?, ?, 0, ?, ?)",
            (
                new_id("job"),
                mode,
                scope,
                dumps(est),
                version - 1,
                version,
                est.get("rows_to_rewrite")
                or est.get("windows_to_recompute")
                or est.get("chains_to_recompute")
                or 0,
                ts,
            ),
        )
    return impact


def seed_mapping(conn):
    """Initial version: every source identity starts as its own entity."""
    cur = current_version(conn)
    if cur:
        return cur
    ts = now_iso()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO adjudication_versions"
        "(version,action,decided_by,base_version,rationale,created_at)"
        " VALUES(1,'seed','system',NULL,'initial 1:1 seeding',?)",
        (ts,),
    )
    for r in conn.execute("SELECT sid FROM source_identities ORDER BY sid"):
        eid = new_id("ent")
        conn.execute(
            "INSERT INTO analysis_entities(eid,created_version,note)"
            " VALUES(?,1,'seed entity')",
            (eid,),
        )
        conn.execute(
            "INSERT INTO entity_membership(sid,eid,valid_from,valid_to,created_at)"
            " VALUES(?,? ,1,NULL,?)",
            (r["sid"], eid, ts),
        )
    conn.commit()
    return 1
