import csv
import tempfile
import unittest
from pathlib import Path

import duckdb

from incident_pipeline.remediation import propose_customer_key_remediation


class RemediationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = self.root / "customer_reference.csv"
        with self.reference.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(["customer_id"])
            writer.writerows([["C-101"], ["C-102"], ["C-103"]])

    def _run_dir(self, rows):
        run_dir = self.root / "run"
        run_dir.mkdir(exist_ok=True)
        database = run_dir / "pipeline.duckdb"
        connection = duckdb.connect(str(database))
        try:
            connection.execute("CREATE TABLE orders_daily(order_id VARCHAR, customer_id VARCHAR)")
            connection.executemany("INSERT INTO orders_daily VALUES (?, ?)", rows)
        finally:
            connection.close()
        (run_dir / "input.csv").write_text("synthetic snapshot\n", encoding="utf-8")
        return run_dir

    def test_verified_upper_trim_candidate_does_not_modify_run_artifacts(self):
        run_dir = self._run_dir([
            ("O-201", "c-101"),
            ("O-202", "C-102"),
            ("O-203", "C-103"),
        ])
        before = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}

        proposal = propose_customer_key_remediation("eval2_d008", run_dir, self.reference)

        after = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(proposal.status, "verified_candidate")
        self.assertEqual(proposal.root_cause_category, "join_reference")
        self.assertEqual(proposal.candidate_name, "upper_trim")
        self.assertTrue(proposal.verification.checks_passed)
        self.assertEqual(proposal.verification.unmatched_keys_before, 1)
        self.assertEqual(proposal.verification.unmatched_keys_after, 0)
        self.assertEqual(proposal.verification.row_count_before, proposal.verification.row_count_after)
        self.assertEqual(proposal.verification.source_normalization_collisions, 0)
        self.assertEqual(proposal.change_preview[0].before, "c-101")
        self.assertEqual(proposal.change_preview[0].after, "C-101")
        self.assertTrue(proposal.approval_required)
        self.assertTrue(proposal.sandbox_only)
        self.assertFalse(proposal.changes_made)

    def test_candidate_is_rejected_when_distinct_source_keys_collapse(self):
        run_dir = self._run_dir([
            ("O-201", "c-101"),
            ("O-202", "C-101"),
        ])
        proposal = propose_customer_key_remediation("collision", run_dir, self.reference)

        self.assertEqual(proposal.status, "no_safe_candidate")
        self.assertFalse(proposal.verification.checks_passed)
        self.assertEqual(proposal.verification.source_normalization_collisions, 1)
        self.assertTrue(any("collapse" in blocker for blocker in proposal.verification.blockers))
        self.assertFalse(proposal.changes_made)

    def test_candidate_is_rejected_when_normalization_does_not_resolve_lookup(self):
        run_dir = self._run_dir([("O-201", "X-999")])
        proposal = propose_customer_key_remediation("unresolved", run_dir, self.reference)

        self.assertEqual(proposal.status, "no_safe_candidate")
        self.assertGreater(proposal.verification.unmatched_keys_after, 0)
        self.assertTrue(any("unmatched" in blocker for blocker in proposal.verification.blockers))

    def test_reference_schema_is_bounded(self):
        bad_reference = self.root / "bad.csv"
        bad_reference.write_text("customer_id,status\nC-101,active\n", encoding="utf-8")
        run_dir = self._run_dir([("O-201", "c-101")])
        with self.assertRaisesRegex(ValueError, "exactly one customer_id"):
            propose_customer_key_remediation("bad-reference", run_dir, bad_reference)


if __name__ == "__main__":
    unittest.main()
