"""Disposable-schema PostgreSQL integration tests; never use DATABASE_URL.

Requires RECON_TEST_DATABASE_URL explicitly. Each test creates its own schema.
"""
import csv
import os
from pathlib import Path
import tempfile
import unittest
import uuid
import contextlib
import io
import json
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql

from recon_engine.db.loader import TABLES, load_dataset
from recon_engine.db.allocations import reserve
from recon_engine.db.save_resolutions import save_run
from recon_engine.matching.deterministic_matcher import MatchResult
from recon_engine.batch import run

DSN = os.environ.get("RECON_TEST_DATABASE_URL")


@unittest.skipUnless(DSN, "Set RECON_TEST_DATABASE_URL to an isolated test database")
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.schema = "test_" + uuid.uuid4().hex
        self.conn = psycopg.connect(DSN, autocommit=True)
        self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.ledger = [["t1", "2026-01-01", "100", "1234", "test", "", "mask", "mcc", "merchant"],
                       ["t2", "2026-01-01", "100", "1234", "test", "", "mask", "mcc", "merchant"],
                       ["t3", "2026-01-01", "60", "1234", "test", "", "mask", "mcc", "merchant"],
                       ["t4", "2026-01-01", "40", "1234", "test", "", "mask", "mcc", "merchant"]]
        self.settlements = [["s1", "2026-01-01", "100", "0", "100", "1234", "test", "b", "", "mask", "mcc", "merchant"],
                            ["s2", "2026-01-01", "100", "0", "100", "1234", "test", "b", "", "mask", "mcc", "merchant"]]
        self.write()

    def tearDown(self):
        self.conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        self.conn.close()
        self.tmp.cleanup()

    def write(self):
        for name, rows in [("internal_ledger", self.ledger), ("network_settlement", self.settlements)]:
            with (self.directory / f"{name}.csv").open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(TABLES[name])
                writer.writerows(rows)

    def load(self):
        return load_dataset(self.conn, self.directory)

    def test_complete_batch_commit_and_partial_approval_rejected(self):
        self.load()
        import copy
        from tests.test_human_approval import fake_state
        cases = []
        group = {"transaction_ids": ["t3", "t4"], "settlement_ids": ["s1"]}
        for tid in group["transaction_ids"]:
            case = copy.deepcopy(fake_state()["exceptions"][0])
            case["internal_txn_id"] = tid
            case["evidence"]["allocation_group"] = group
            case["evidence"]["human_decision"] = "approved"
            case["evidence"]["resolution_proposal"].update(
                internal_txn_id=tid, resolution_type="confirmed_batch", matched_settlement_line_ids=["s1"])
            cases.append(case)
        partial = copy.deepcopy(cases)
        partial[1]["evidence"]["human_decision"] = "rejected"
        with self.assertRaises(ValueError):
            save_run("partial", "test", "test", partial, conn=self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM allocated_transactions").fetchone()[0], 0)
        self.assertEqual(save_run("complete", "test", "test", cases, conn=self.conn), 2)
        self.assertEqual(save_run("complete", "test", "test", cases, conn=self.conn), 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM allocated_transactions").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM allocated_settlements").fetchone()[0], 1)

    def test_replay_does_not_duplicate_or_drop(self):
        self.assertEqual(self.load()["inserted"]["internal_ledger"], 4)
        self.assertTrue(self.load()["replayed"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM internal_ledger").fetchone()[0], 4)

    def test_correction_rolls_back_entire_batch(self):
        self.load()
        self.ledger.append(["new", *self.ledger[0][1:]])
        self.settlements[0][2] = "101"
        self.write()
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM internal_ledger").fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0], 1)

    def test_identical_duplicate_delivery_collapses(self):
        self.ledger.append(self.ledger[0].copy())
        self.write()
        self.assertEqual(self.load()["inserted"]["internal_ledger"], 4)

    def test_conflicting_duplicate_rejected(self):
        changed = self.ledger[0].copy()
        changed[2] = "105"
        self.ledger.append(changed)
        self.write()
        with self.assertRaises(ValueError):
            self.load()

    def test_header_contract_is_enforced(self):
        p = self.directory / "internal_ledger.csv"
        p.write_text(p.read_text().replace("account_last4,merchant_descriptor", "merchant_descriptor,account_last4", 1))
        with self.assertRaises(ValueError):
            self.load()

    def test_nonfinite_money_rejected(self):
        self.ledger[0][2] = "NaN"
        self.write()
        with self.assertRaises(ValueError):
            self.load()

    def test_allocation_replay_and_cross_run_conflict(self):
        self.load()
        with self.conn.transaction():
            first = reserve(self.conn, ["t1"], ["s1"])
        with self.conn.transaction():
            self.assertEqual(reserve(self.conn, ["t1"], ["s1"]), first)
        with self.assertRaises(ValueError), self.conn.transaction():
            reserve(self.conn, ["t2"], ["s1"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM allocation_groups").fetchone()[0], 1)

    def test_explicit_balanced_batch_and_invalid_group(self):
        self.load()
        with self.conn.transaction():
            reserve(self.conn, ["t3", "t4"], ["s1"])
        with self.assertRaises(ValueError), self.conn.transaction():
            reserve(self.conn, ["t1", "t2"], ["s2"])

    def test_unknown_source_rejected(self):
        self.load()
        with self.assertRaises(ValueError), self.conn.transaction():
            reserve(self.conn, ["missing"], ["s1"])

    def test_run_replay_and_changed_payload(self):
        self.load()
        matches = [MatchResult("t1", "exact_one_to_one", ["s1"])]
        save_run("r1", "none", "none", [], matches, conn=self.conn)
        self.assertEqual(save_run("r1", "none", "none", [], matches, conn=self.conn), 0)
        with self.assertRaises(ValueError):
            save_run("r1", "none", "none", [], [], conn=self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM reconciliation_runs").fetchone()[0], 1)

    def test_batch_failure_recovery_and_metrics(self):
        output = self.directory / "reports"
        scoped_dsn = psycopg.conninfo.make_conninfo(DSN, options=f"-c search_path={self.schema}")
        with patch.dict(os.environ, {"DATABASE_URL": scoped_dsn}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                run(self.directory, output, "retry-me", inject_failure=True)
            self.assertIn("recon_batch_success 0", (output / "metrics.prom").read_text())
            self.assertEqual(self.conn.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0], 1)
            result = run(self.directory, output, "retry-me")
            self.assertEqual(result["status"], "succeeded")
            self.assertIn("recon_batch_success 1", (output / "metrics.prom").read_text())
            previous_success = result["last_success_timestamp"]
            with self.assertRaises(RuntimeError):
                run(self.directory, output, "retry-next", inject_failure=True)
            self.assertEqual(json.loads((output / "status.json").read_text())["last_success_timestamp"], previous_success)

    def test_proposal_audit_and_allocation_commit_together(self):
        self.load()
        def case(txn):
            return {"internal_txn_id": txn, "amount": 100, "evidence": {
                "human_decision": "approved", "resolution_proposal": {
                    "internal_txn_id": txn, "resolution_type": "confirmed_match",
                    "matched_settlement_line_ids": ["s1"], "confidence": 0.9,
                    "reasoning": "fixture", "requires_human_approval": True}}}
        save_run("agent1", "fixture", "fixture", [case("t1")], conn=self.conn)
        self.assertEqual(save_run("agent1", "fixture", "fixture", [case("t1")], conn=self.conn), 0)
        with self.assertRaises(ValueError):
            save_run("agent2", "fixture", "fixture", [case("t2")], conn=self.conn)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM agent_resolutions").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM reconciliation_runs").fetchone()[0], 1)

    def test_concurrent_conflicting_allocations_have_one_winner(self):
        self.load()
        def attempt(txn):
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
                try:
                    with conn.transaction():
                        reserve(conn, [txn], ["s1"])
                    return "accepted"
                except ValueError:
                    return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["t1", "t2"]))
        self.assertCountEqual(results, ["accepted", "conflict"])


if __name__ == "__main__":
    unittest.main()
