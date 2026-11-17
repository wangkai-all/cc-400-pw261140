"""End-to-end invariants on a temporary synthetic database."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from idres import adjudication as adj
from idres import rebuilder as rb
from idres import reporting as rep
from idres.db import connect
from idres.synth import generate, inject_event


class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.summary = generate(self.db, seed=2611)
        self.conn = connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def drain(self):
        # Run every queued job; each worker_tick claims one job.
        for _ in range(20):
            if rb.worker_tick(self.conn) is None:
                break

    # ------------------------------------------------------------- 1 seeding
    def test_01_seed_and_initial_build(self):
        self.assertEqual(adj.current_version(self.conn), 1)
        self.drain()
        f = rep.freshness_dashboard(self.conn)
        self.assertEqual(f["pending_events"], 0)
        self.assertEqual(f["pointwise"]["stale"], 0)
        self.assertEqual(f["interval"]["stale"], 0)
        total = self.conn.execute(
            "SELECT COUNT(*) n FROM source_events"
        ).fetchone()["n"]
        self.assertEqual(f["pointwise"]["fresh"], total)

    # ------------------------------------------------------------- 2 merge
    def test_02_merge_obsoletes_metrics_and_enqueues_jobs(self):
        self.drain()
        sid_a, sid_b = self.summary["trap_a"]
        before = adj.impact_preview(
            self.conn, [sid_a, sid_b],
            list({adj.current_mapping(self.conn)[sid_a],
                  adj.current_mapping(self.conn)[sid_b]}),
        )
        self.assertTrue(before["pointwise"]["repairable_locally"])
        self.assertFalse(before["interval"]["repairable_locally"])
        r = adj.commit_decision(
            self.conn, "merge", 1, "tester", "merge on weak evidence",
            merge_groups=[[sid_a, sid_b]],
        )
        self.assertTrue(r["accepted"])
        self.assertEqual(r["version"], 2)
        # Mapping changed, and pointwise rows of moved sids went stale —
        # no way to leave a wrong dedup metric looking fresh.
        f = rep.freshness_dashboard(self.conn)
        self.assertGreater(f["pointwise"]["stale"], 0)
        jobs = rb.list_jobs(self.conn)
        modes = {j["mode"] for j in jobs[:3]}
        self.assertEqual(modes, {"pointwise", "interval", "chain"})

    # ------------------------------------------------------------- 3 CAS
    def test_03_late_old_decision_is_refused_and_audited(self):
        self.drain()
        sid_a, sid_b = self.summary["trap_a"]
        adj.commit_decision(
            self.conn, "merge", 1, "tester", "v2 merge",
            merge_groups=[[sid_a, sid_b]],
        )
        # Newer conclusion first: split them apart again (v3).
        mapping = adj.current_mapping(self.conn)
        ent = mapping[sid_b]
        split = adj.commit_decision(
            self.conn, "split", 2, "tester", "v3 split",
            split_specs=[{"sids": [sid_b], "note": "undo"}],
        )
        self.assertTrue(split["accepted"])
        self.assertEqual(adj.current_version(self.conn), 3)
        # A late worker still holding base_version=2 must not win.
        late = adj.commit_decision(
            self.conn, "merge", 2, "late-worker", "stale re-merge",
            merge_groups=[[sid_a, sid_b]],
        )
        self.assertFalse(late["accepted"])
        self.assertEqual(late["current_version"], 3)
        audit = adj.stale_decisions(self.conn)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["attempted_version"], 2)
        # And the mapping really is still the v3 split.
        self.assertNotEqual(
            adj.current_mapping(self.conn)[sid_a],
            adj.current_mapping(self.conn)[sid_b],
        )

    # ------------------------------------------------- 4 split local repair
    def test_04_split_pointwise_only_rewrites_affected_rows(self):
        self.drain()
        sid_a, sid_b = self.summary["trap_a"]
        adj.commit_decision(
            self.conn, "merge", 1, "tester", "merge",
            merge_groups=[[sid_a, sid_b]],
        )
        impact = rb.list_jobs(self.conn)
        pw_job = next(j for j in impact if j["mode"] == "pointwise")
        affected = pw_job["impact"]["rows_to_rewrite"]
        total = self.conn.execute(
            "SELECT COUNT(*) n FROM source_events"
        ).fetchone()["n"]
        self.assertLess(affected, total)  # local, not full table
        self.drain()
        mapping = adj.current_mapping(self.conn)
        ent_b = mapping[sid_b]
        split = adj.commit_decision(
            self.conn, "split", 2, "tester", "undo merge",
            split_specs=[{"sids": [sid_b], "note": "contradicting email"}],
        )
        self.assertTrue(split["accepted"])
        # Superseded entity handling: the merged entity may now be empty...
        # rebuild drains, and every pointwise row becomes fresh again.
        self.drain()
        f = rep.freshness_dashboard(self.conn)
        self.assertEqual(f["pointwise"]["stale"], 0)
        self.assertEqual(f["pending_events"], 0)
        # The two sids map to different entities once more.
        self.assertNotEqual(
            adj.current_mapping(self.conn)[sid_a],
            adj.current_mapping(self.conn)[sid_b],
        )

    # --------------------------------------------- 5 interruption + resume
    def test_05_interrupt_resumes_from_cursor_without_rework(self):
        self.drain()
        sid_a, sid_b = self.summary["trap_a"]
        adj.commit_decision(
            self.conn, "merge", 1, "tester", "merge",
            merge_groups=[[sid_a, sid_b]],
        )
        # Interrupt after the first completed batch (check fires at the top
        # of the next iteration, so exactly one batch is committed). Shrink
        # the batch size so the job cannot finish in a single batch.
        old_batch = rb.BATCH
        rb.BATCH = 3
        job = next(j for j in rb.list_jobs(self.conn) if j["mode"] == "pointwise")
        stops = {"n": 0}

        def stop_after_one_batch():
            stops["n"] += 1
            return stops["n"] > 1

        try:
            rb.worker_tick(self.conn, stop_check=stop_after_one_batch)
        finally:
            rb.BATCH = old_batch
        job2 = rb.get_job(self.conn, job["job_id"])
        self.assertEqual(job2["status"], "interrupted")
        self.assertIsNotNone(job2["cursor_event"])
        partial = job2["processed"]
        self.assertGreater(partial, 0)
        # Resume and finish; processed count continues, no restart from zero.
        self.assertTrue(rb.resume_job(self.conn, job["job_id"]))
        self.drain()
        done = rb.get_job(self.conn, job["job_id"])
        self.assertEqual(done["status"], "done")
        f = rep.freshness_dashboard(self.conn)
        self.assertEqual(f["pointwise"]["stale"], 0)

    # ------------------------------------ 6 events arriving mid-rebuild
    def test_06_new_event_during_rebuild_is_not_lost(self):
        self.drain()
        before = self.conn.execute(
            "SELECT COUNT(*) n FROM source_events"
        ).fetchone()["n"]
        new_id = inject_event(self.db, event_type="purchase", amount=42.5)
        # Seed jobs already done; a fresh pointwise run picks the new event
        # through the cursor scan even though no new decision was made.
        self.conn.execute(
            "INSERT INTO rebuild_jobs"
            "(job_id,mode,status,impact_scope,impact_json,from_version,to_version,"
            "processed,total,created_at) VALUES"
            "('job-catchup','pointwise','queued','full','{}',1,1,0,0,?)",
            ("2026-11-17T00:00:00Z",),
        )
        self.conn.commit()
        self.drain()
        row = self.conn.execute(
            "SELECT is_fresh FROM event_attribution WHERE event_id=?",
            (new_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_fresh"], 1)
        after = self.conn.execute(
            "SELECT COUNT(*) n FROM source_events"
        ).fetchone()["n"]
        self.assertEqual(after, before + 1)

    # ------------------------------------------------ 7 report provenance
    def test_07_report_traces_to_version_and_source_events(self):
        self.drain()
        report_id, body = rep.build_entity_report(self.conn)
        self.assertEqual(body["adjudication_version"], 1)
        ent = next(e for e in body["entities"] if e["event_count"] > 0)
        self.assertTrue(ent["provenance"]["source_event_ids"])
        first_event = ent["provenance"]["source_event_ids"][0]
        trace = rep.trace_event(self.conn, first_event)
        self.assertEqual(trace["source_event"]["event_id"], first_event)
        self.assertTrue(trace["membership_timeline"])
        self.assertIsNotNone(trace["attribution"])
        self.assertTrue(trace["interval_snapshots"])  # aggregate provenance
        stored = rep.get_report(self.conn, report_id)
        self.assertEqual(stored["built_version"], 1)

    # ----------------------------------- 8 interval cannot locally repair
    def test_08_interval_rebuild_recomputes_whole_windows(self):
        self.drain()
        sid_a, sid_b = self.summary["trap_a"]
        adj.commit_decision(
            self.conn, "merge", 1, "tester", "merge",
            merge_groups=[[sid_a, sid_b]],
        )
        job = next(j for j in rb.list_jobs(self.conn) if j["mode"] == "interval")
        self.assertFalse(job["impact_scope"] == "local")
        self.drain()
        # Every surviving snapshot remains linked to concrete source events.
        orphans = self.conn.execute(
            "SELECT COUNT(*) n FROM interval_snapshots s "
            "LEFT JOIN interval_snapshot_events e ON e.snap_id=s.snap_id "
            "WHERE e.event_id IS NULL AND s.event_count>0"
        ).fetchone()["n"]
        self.assertEqual(orphans, 0)

    # --------------------------------------------- 9 chain blast radius
    def test_09_behavior_chains_rebuilt_after_split(self):
        self.drain()
        chains_before = self.conn.execute(
            "SELECT COUNT(*) n FROM behavior_chains"
        ).fetchone()["n"]
        sid_a, sid_b = self.summary["trap_a"]
        adj.commit_decision(
            self.conn, "merge", 1, "tester", "merge",
            merge_groups=[[sid_a, sid_b]],
        )
        self.drain()
        adj.commit_decision(
            self.conn, "split", 2, "tester", "split",
            split_specs=[{"sids": [sid_b]}],
        )
        self.drain()
        # Chain job was enqueued with full scope and completes fresh.
        f = rep.freshness_dashboard(self.conn)
        self.assertEqual(f["chain"]["stale"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) n FROM behavior_chains WHERE is_fresh=0"
            ).fetchone()["n"],
            0,
        )
        self.assertGreaterEqual(chains_before, 1)


    # --------------------------- 10 interrupt flag before job is claimed
    def test_10_interrupt_before_claim_lands_as_interrupted(self):
        self.drain()
        sid_a, sid_b = self.summary["trap_a"]
        adj.commit_decision(
            self.conn, "merge", 1, "tester", "merge",
            merge_groups=[[sid_a, sid_b]],
        )
        job = next(j for j in rb.list_jobs(self.conn) if j["mode"] == "pointwise")
        # Flag it while it is still queued; the worker's sweep must settle it.
        self.assertTrue(rb.interrupt_job(self.conn, job["job_id"]))
        ran = rb.worker_tick(self.conn)  # sweeps, then claims another job
        self.assertEqual(rb.get_job(self.conn, job["job_id"])["status"],
                         "interrupted")
        # And resume works on the settled job.
        self.assertTrue(rb.resume_job(self.conn, job["job_id"]))
        self.drain()
        self.assertEqual(
            rb.get_job(self.conn, job["job_id"])["status"], "done"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
