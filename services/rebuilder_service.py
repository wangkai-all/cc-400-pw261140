"""Contribution rebuilder service (independent process, port 8102).

A background worker drains queued jobs. The web API can:

* GET  /jobs                 queue state incl. cursor/processed totals
* POST /jobs/<id>/interrupt  cooperative stop at next batch boundary
* POST /jobs/<id>/resume     re-queue an interrupted job
* POST /tick                 run one job synchronously (handy for demos/tests)
* POST /ingest               inject a new fictional event mid-rebuild

Interruption never loses progress: the cursor is committed per batch, and the
batch queries are cursor-only, so events ingested after the job started are
picked up without a restart (high-water semantics).
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idres import rebuilder as rb
from idres.db import connect
from idres.httpapp import App
from idres.synth import inject_event

DB = os.environ.get("IDRES_DB", "data/idres.db")
app = App(DB, "rebuilder")
_stop_worker = threading.Event()


@app.route("GET", r"/health")
def health(conn, body, p, q):
    return {"service": "rebuilder", "ok": True}


@app.route("GET", r"/jobs")
def jobs(conn, body, p, q):
    return {"jobs": rb.list_jobs(conn)}


@app.route("POST", r"/jobs/(?P<job_id>[^/]+)/interrupt")
def interrupt(conn, body, p, q):
    ok = rb.interrupt_job(conn, p["job_id"])
    return (200 if ok else 409), {"ok": ok, "job_id": p["job_id"]}


@app.route("POST", r"/jobs/(?P<job_id>[^/]+)/resume")
def resume(conn, body, p, q):
    ok = rb.resume_job(conn, p["job_id"])
    return (200 if ok else 409), {"ok": ok, "job_id": p["job_id"]}


@app.route("POST", r"/tick")
def tick(conn, body, p, q):
    job_id = rb.worker_tick(conn)
    return {"ran_job": job_id}


@app.route("POST", r"/ingest")
def ingest(conn, body, p, q):
    # Reuse the module's helper through its own short-lived connection.
    event_id = inject_event(
        DB,
        event_type=body.get("event_type", "login"),
        amount=body.get("amount"),
        note=body.get("note"),
    )
    jobs = rb.enqueue_catchup(conn)
    return {"ingested": event_id, "enqueued_jobs": jobs,
            "note": "new event visible to running rebuild via cursor scan; "
                    "catch-up jobs enqueued if the queue was idle"}


def worker_loop():
    """Drain the queue; idle-polls so newly enqueued jobs start promptly."""
    while not _stop_worker.is_set():
        conn = connect(DB)
        try:
            job_id = rb.worker_tick(conn, stop_check=_stop_worker.is_set)
        except Exception as exc:  # pragma: no cover
            print(f"[rebuilder] worker error: {exc}", flush=True)
            job_id = None
        finally:
            conn.close()
        if job_id is None:
            _stop_worker.wait(0.4)
        else:
            print(f"[rebuilder] finished {job_id}", flush=True)


if __name__ == "__main__":
    t = threading.Thread(target=worker_loop, daemon=True, name="rebuilder")
    t.start()
    app.serve(os.environ.get("RB_PORT", "8102"))
