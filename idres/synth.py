"""Synthetic identity/event generator — *all data is fictional*.

There is deliberately no real-world identity resolution here: names are drawn
from a fixed in-memory word list, ids are random, and every row is tagged
``data_kind=synthetic`` in the ``meta`` table.

Scenario shape
--------------
* 8 fictional persons, each observed in 2-3 source systems (~18 source
  identities), with deterministic cross-system evidence (shared email/device).
* Two planted traps for the merge-then-split workflow:
    - person #4: same device + name/geo similarity (weak evidence), but
      contradicting email -> the merge should later be undone;
    - person #7: two accounts that genuinely share an email but where a late
      stale decision tries to re-merge after the split (CAS demonstration).
* Events span four months so interval snapshots have multiple windows; device
  ids are shared across distinct persons to create genuine cross-entity
  behaviour chains.
"""
from __future__ import annotations

import json
import random

from .adjudication import seed_mapping
from .db import connect, init_db
from .rebuilder import enqueue_seed_jobs
from .util import dumps, new_id, now_iso

SYSTEMS = ["crm", "billing", "support"]
FIRST = ["Aria", "Bram", "Cleo", "Dov", "Elin", "Fenn", "Gus", "Hana"]
LAST = ["Vale", "North", "Wren", "Marsh", "Lind", "Okafor", "Pike", "Sol"]
CITIES = ["Lakeside", "Ironford", "Greenhollow", "Brampton", "Westmere"]
EVENT_TYPES = [
    ("login", None),
    ("purchase", "amount"),
    ("refund", "amount"),
    ("ticket", None),
    ("payout", "amount"),
]


def _fictional_email(rng, i, tag):
    return f"user{i}-{tag}@example.test"


def generate(db_path: str, seed: int = 2611):
    rng = random.Random(seed)
    init_db(db_path)
    conn = connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    ts0 = "2026-08-01T08:00:00.000Z"

    persons = []
    sid_counter = 0
    all_sids = []

    for i in range(8):
        name = f"{FIRST[i]} {LAST[i]}"
        base_email = _fictional_email(rng, i, "main")
        devices = [f"dev-{rng.randint(1000, 9999)}" for _ in range(2)]
        n_systems = 3 if i in (0, 3, 6) else 2
        sids = []
        for j in range(n_systems):
            sid_counter += 1
            sid = f"sid-{sid_counter:04d}"
            all_sids.append(sid)
            # Same canonical email across systems for true matches, except
            # person #4 whose support account uses a different email domain.
            if i == 3 and j == 2:
                email = f"other{i}@mail.example.test"
            else:
                email = base_email
            traits = {
                "email": email,
                "device_id": devices[j % len(devices)],
                "city": CITIES[(i + j) % len(CITIES)],
                "fictional": True,
            }
            conn.execute(
                "INSERT INTO source_identities"
                "(sid,source_system,external_ref,display_name,traits_json,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    sid,
                    SYSTEMS[j],
                    f"ref-{i+1}-{j+1}",
                    name,
                    dumps(traits),
                    ts0,
                ),
            )
            sids.append(sid)
        persons.append({"name": name, "sids": sids, "devices": devices,
                        "email": base_email})

    # ------------------------------ evidence: strong shared email/device pairs
    ev_ts = "2026-08-02T09:00:00.000Z"
    for p in persons:
        for a, b in zip(p["sids"], p["sids"][1:]):
            conn.execute(
                "INSERT INTO candidate_evidence"
                "(evidence_id,sid_a,sid_b,evidence_type,score,detail_json,found_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("ev"),
                    a,
                    b,
                    "same_email",
                    0.92,
                    dumps({"shared": "email", "value": p["email"]}),
                    ev_ts,
                ),
            )
            conn.execute(
                "INSERT INTO candidate_evidence"
                "(evidence_id,sid_a,sid_b,evidence_type,score,detail_json,found_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    new_id("ev"),
                    a,
                    b,
                    "same_device",
                    0.85,
                    dumps({"shared": "device_id"}),
                    ev_ts,
                ),
            )

    # Trap A: persons #3 and #4 share a device and have name/geo similarity,
    # but emails contradict -> weak evidence that looks mergeable.
    trap_a, trap_b = persons[3], persons[4]
    shared_device = trap_a["devices"][0]
    conn.execute(
        "INSERT INTO candidate_evidence"
        "(evidence_id,sid_a,sid_b,evidence_type,score,detail_json,found_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            new_id("ev"),
            trap_a["sids"][0],
            trap_b["sids"][0],
            "name_geo_plus_device",
            0.58,
            dumps(
                {
                    "shared_device": shared_device,
                    "name_similarity": 0.61,
                    "geo": "same_city",
                    "warning": "weak, circumstantial evidence",
                }
            ),
            ev_ts,
        ),
    )
    conn.execute(
        "INSERT INTO candidate_evidence"
        "(evidence_id,sid_a,sid_b,evidence_type,score,detail_json,found_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            new_id("ev"),
            trap_a["sids"][0],
            trap_b["sids"][0],
            "contradicting_email",
            0.15,
            dumps(
                {
                    "email_a": trap_a["email"],
                    "email_b": trap_b["email"],
                    "warning": "different primary emails — likely distinct people",
                }
            ),
            ev_ts,
        ),
    )

    # ------------------------------------------------- events across 4 months
    months = ["2026-08", "2026-09", "2026-10", "2026-11"]
    event_n = 0
    for p_idx, p in enumerate(persons):
        for m_idx, month in enumerate(months):
            n_events = rng.randint(3, 6)
            for _ in range(n_events):
                event_n += 1
                etype, amount_kind = EVENT_TYPES[rng.randrange(len(EVENT_TYPES))]
                amount = (
                    round(rng.uniform(5, 900), 2) if amount_kind else None
                )
                sid = rng.choice(p["sids"])
                # Cross-entity chains: persons 2,3,4 occasionally reuse the
                # trap device on purpose so chains span multiple entities.
                if p_idx in (2, 3, 4) and rng.random() < 0.35:
                    device = shared_device
                else:
                    device = rng.choice(p["devices"])
                day = rng.randint(1, 26)
                occurred = f"{month}-{day:02d}T{rng.randint(8,20):02d}:" \
                           f"{rng.randint(0,59):02d}:00.000Z"
                conn.execute(
                    "INSERT INTO source_events"
                    "(event_id,sid,event_type,amount,occurred_at,ingested_at,"
                    "payload_json)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        f"ev-{event_n:06d}",
                        sid,
                        etype,
                        amount,
                        occurred,
                        now_iso(),
                        dumps(
                            {
                                "device_id": device,
                                "channel": rng.choice(["web", "mobile", "phone"]),
                                "fictional": True,
                            }
                        ),
                    ),
                )

    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('data_kind','synthetic')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('generator_seed',?)",
        (str(seed),),
    )
    conn.commit()

    version = seed_mapping(conn)
    enqueue_seed_jobs(conn, version)
    conn.close()
    return {
        "persons": len(persons),
        "source_identities": sid_counter,
        "events": event_n,
        "trap_a": [trap_a["sids"][0], trap_b["sids"][0]],
    }


def inject_event(db_path: str, event_type="login", amount=None, note=None):
    """Ingest one new fictional event (used by the UI 'new event during
    rebuild' control). Takes the next zero-padded event id so the rebuilder
    cursor/high-water logic sees it immediately."""
    conn = connect(db_path)
    conn.execute("BEGIN IMMEDIATE")
    nxt = conn.execute(
        "SELECT COALESCE(MAX(CAST(substr(event_id,4) AS INTEGER)),0)+1 n "
        "FROM source_events"
    ).fetchone()["n"]
    sid_row = conn.execute(
        "SELECT sid FROM source_identities ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    traits = conn.execute(
        "SELECT traits_json FROM source_identities WHERE sid=?",
        (sid_row["sid"],),
    ).fetchone()
    device = json.loads(traits["traits_json"]).get("device_id")
    event_id = f"ev-{nxt:06d}"
    conn.execute(
        "INSERT INTO source_events"
        "(event_id,sid,event_type,amount,occurred_at,ingested_at,payload_json)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            event_id,
            sid_row["sid"],
            event_type,
            amount,
            now_iso(),
            now_iso(),
            dumps({"device_id": device, "note": note or "live-injected",
                   "fictional": True}),
        ),
    )
    conn.commit()
    conn.close()
    return event_id
