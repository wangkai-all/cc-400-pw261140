"""Reporting service (independent process, port 8103).

All reports are stored with the adjudication version they were built on, and
every figure is traceable to source events:

GET  /health
GET  /freshness
POST /reports/entity        build (and persist) the full entity summary
GET  /reports               list stored reports
GET  /reports/<id>          fetch one stored report
GET  /trace/<event_id>      provenance drill-down for one source event
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idres import reporting as rep
from idres.httpapp import App

DB = os.environ.get("IDRES_DB", "data/idres.db")
app = App(DB, "reporting")


@app.route("GET", r"/health")
def health(conn, body, p, q):
    return {"service": "reporting", "ok": True, **rep.freshness_dashboard(conn)}


@app.route("GET", r"/freshness")
def freshness(conn, body, p, q):
    return rep.freshness_dashboard(conn)


@app.route("POST", r"/reports/entity")
def build_report(conn, body, p, q):
    report_id, payload = rep.build_entity_report(conn)
    return {"report_id": report_id, "report": payload}


@app.route("GET", r"/reports")
def list_reports(conn, body, p, q):
    return {"reports": rep.list_reports(conn)}


@app.route("GET", r"/reports/(?P<report_id>[^/]+)")
def one_report(conn, body, p, q):
    out = rep.get_report(conn, p["report_id"])
    return (200 if out else 404), out or {"error": "not found"}


@app.route("GET", r"/trace/(?P<event_id>[^/]+)")
def trace(conn, body, p, q):
    out = rep.trace_event(conn, p["event_id"])
    return (200 if out else 404), out or {"error": "not found"}


if __name__ == "__main__":
    app.serve(os.environ.get("RPT_PORT", "8103"))
