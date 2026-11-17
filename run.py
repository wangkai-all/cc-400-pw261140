#!/usr/bin/env python3
"""Command-line entry point.

Usage:
  python3 run.py init-db [--seed 2611] [--db data/idres.db]
  python3 run.py serve adj|rb|rpt|web
  python3 run.py drain            # synchronously run all queued rebuild jobs
  python3 run.py smoke            # end-to-end HTTP smoke test
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from idres.db import connect
from idres import rebuilder as rb
from idres.synth import generate

PORTS = {"web": 8100, "adj": 8101, "rb": 8102, "rpt": 8103}


def cmd_init(args):
    summary = generate(args.db, seed=args.seed)
    print(json.dumps({"db": args.db, **summary}, ensure_ascii=False, indent=2))


def cmd_serve(args):
    which = args.which
    os.environ["IDRES_DB"] = args.db
    if which == "adj":
        from services.adjudication_service import app
        app.serve(PORTS["adj"])
    elif which == "rb":
        from services.rebuilder_service import worker_loop
        import threading
        threading.Thread(target=worker_loop, daemon=True).start()
        from services.rebuilder_service import app
        app.serve(PORTS["rb"])
    elif which == "rpt":
        from services.reporting_service import app
        app.serve(PORTS["rpt"])
    elif which == "web":
        from services.web_service import WebHandler
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("0.0.0.0", PORTS["web"]), WebHandler)
        print(f"[web] console on http://127.0.0.1:{PORTS['web']}", flush=True)
        httpd.serve_forever()


def cmd_drain(args):
    conn = connect(args.db)
    n = 0
    while rb.worker_tick(conn):
        n += 1
    print(f"drained {n} jobs")
    conn.close()


def _call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def cmd_smoke(args):
    base = {k: f"http://127.0.0.1:{p}" for k, p in PORTS.items()}
    for name in ("adj", "rb", "rpt"):
        _call("GET", f"{base[name]}/health")
    print("health ok")
    v = _call("GET", f"{base['adj']}/version")["version"]
    ev = _call("GET", f"{base['adj']}/evidence?status=open")["evidence"]
    pair = next(e for e in ev if e["evidence_type"] == "name_geo_plus_device")
    impact = _call("POST", f"{base['adj']}/impact",
                   {"sids": [pair["sid_a"], pair["sid_b"]]})
    assert impact["pointwise"]["repairable_locally"] is True
    print("impact preview ok:", json.dumps(impact["pointwise"]))
    merged = _call("POST", f"{base['adj']}/merge",
                   {"base_version": v, "group": [pair["sid_a"], pair["sid_b"]]})
    print("merge -> v", merged["version"])
    time.sleep(1.5)  # let the rebuilder worker drain
    split = _call("POST", f"{base['adj']}/split",
                  {"base_version": merged["version"],
                   "specs": [{"sids": [pair["sid_b"]]}]})
    print("split -> v", split["version"])
    late = None
    try:
        _call("POST", f"{base['adj']}/late",
              {"group": [pair["sid_a"], pair["sid_b"]], "delay_versions": 1})
    except urllib.error.HTTPError as e:
        late = json.loads(e.read())
    assert late and late["accepted"] is False
    print("stale decision refused:", late["reason"])
    time.sleep(1.5)
    injected = _call("POST", f"{base['rb']}/ingest",
                     {"event_type": "purchase", "amount": 99.9})["ingested"]
    print("ingested", injected)
    time.sleep(1.5)
    report = _call("POST", f"{base['rpt']}/reports/entity", {})
    assert report["report"]["adjudication_version"] >= split["version"]
    trace = _call("GET", f"{base['rpt']}/trace/{injected}")
    assert trace["attribution"]["is_fresh"] == 1
    print("report + trace ok; report id", report["report_id"])
    print("SMOKE OK")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("IDRES_DB", "data/idres.db"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init-db")
    p.add_argument("--seed", type=int, default=2611)
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("serve")
    p.add_argument("which", choices=list(PORTS))
    p.set_defaults(func=cmd_serve)
    sub.add_parser("drain").set_defaults(func=cmd_drain)
    sub.add_parser("smoke").set_defaults(func=cmd_smoke)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
