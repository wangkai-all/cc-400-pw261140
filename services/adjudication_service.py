"""Adjudication service (independent process, port 8101).

Endpoints
---------
GET  /health
GET  /version
GET  /entities
GET  /evidence?status=open
GET  /evidence/<sid>/history
GET  /stale
POST /impact            {sids:[...]}
POST /decisions/merge   {base_version, group:[...], decided_by, rationale}
POST /decisions/split   {base_version, specs:[{sids:[...],note}], ...}
POST /decisions/late    {delay_versions, ...}  -> simulates a stale worker
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idres import adjudication as adj
from idres.httpapp import App

DB = os.environ.get("IDRES_DB", "data/idres.db")
app = App(DB, "adjudication")


@app.route("GET", r"/health")
def health(conn, body, p, q):
    return {"service": "adjudication", "ok": True,
            "version": adj.current_version(conn)}


@app.route("GET", r"/version")
def version(conn, body, p, q):
    return {"version": adj.current_version(conn)}


@app.route("GET", r"/entities")
def entities(conn, body, p, q):
    return {"version": adj.current_version(conn),
            "entities": adj.list_entities(conn)}


@app.route("GET", r"/evidence")
def evidence(conn, body, p, q):
    return {"evidence": adj.list_evidence(conn, q.get("status"))}


@app.route("GET", r"/evidence/(?P<sid>[^/]+)/history")
def history(conn, body, p, q):
    return {"sid": p["sid"], "history": adj.membership_history(conn, p["sid"])}


@app.route("GET", r"/stale")
def stale(conn, body, p, q):
    return {"stale_decisions": adj.stale_decisions(conn)}


@app.route("POST", r"/impact")
def impact(conn, body, p, q):
    mapping = adj.current_mapping(conn)
    sids = body.get("sids", [])
    eids = {mapping.get(s) for s in sids if mapping.get(s)}
    return adj.impact_preview(conn, sids, list(eids))


@app.route("POST", r"/merge")
def merge(conn, body, p, q):
    result = adj.commit_decision(
        conn,
        action="merge",
        base_version=int(body["base_version"]),
        decided_by=body.get("decided_by", "web-user"),
        rationale=body.get("rationale", "manual merge"),
        merge_groups=[body["group"]],
    )
    return (200 if result["accepted"] else 409), result


@app.route("POST", r"/split")
def split(conn, body, p, q):
    result = adj.commit_decision(
        conn,
        action="split",
        base_version=int(body["base_version"]),
        decided_by=body.get("decided_by", "web-user"),
        rationale=body.get("rationale", "undo erroneous merge"),
        split_specs=body["specs"],
    )
    return (200 if result["accepted"] else 409), result


@app.route("POST", r"/late")
def late(conn, body, p, q):
    """Simulate a worker that computed a merge against an old base_version and
    reports it late. The CAS layer must refuse it and log it."""
    cur = adj.current_version(conn)
    stale_base = max(0, cur - int(body.get("delay_versions", 1)))
    result = adj.commit_decision(
        conn,
        action="merge",
        base_version=stale_base,
        decided_by="simulated-late-worker",
        rationale=body.get("rationale", "late-arriving old decision"),
        merge_groups=[body["group"]],
    )
    return (200 if result["accepted"] else 409), result


if __name__ == "__main__":
    app.serve(os.environ.get("ADJ_PORT", "8101"))
