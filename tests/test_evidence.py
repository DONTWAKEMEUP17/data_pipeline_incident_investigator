import json
import tempfile
import unittest
from pathlib import Path

import duckdb

from incident_pipeline.baseline import run_baseline
from incident_pipeline.evidence import (
    MAX_CELL_CHARS,
    MAX_LOG_LINE_CHARS,
    LocalArtifactProvider,
)
from incident_pipeline.pipeline import generate_runs


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        generate_runs(self.root)
        self.provider = LocalArtifactProvider(self.root)

    def test_known_run_evidence_and_baseline(self):
        healthy = self.provider.get_run_summary("healthy_001")
        failed = self.provider.get_run_summary("failed_001")
        self.assertEqual(healthy.status, "success")
        self.assertEqual(failed.validation_status, "skipped")
        comparison = self.provider.compare_schema("failed_001", "raw_orders")
        self.assertFalse(comparison.matches)
        self.assertIn("amount", [column.name for column in comparison.expected])
        self.assertIn("total_amount", [column.name for column in comparison.observed])
        self.assertIsNone(self.provider.compare_schema("failed_001", "orders_daily").observed)
        profile = self.provider.profile_table("failed_001", "raw_orders")
        self.assertEqual(profile.row_count, 3)
        self.assertEqual(profile.duplicate_order_ids, 0)
        self.assertEqual(profile.null_counts["total_amount"], 0)
        self.assertEqual(run_baseline(self.provider, "failed_001").source, "transform")
        self.assertEqual(run_baseline(self.provider, "healthy_001").finding, "No failure found")

    def test_baseline_prefers_first_failed_validation(self):
        path = self.root / "healthy_001" / "validation.json"
        validation = json.loads(path.read_text())
        validation["checks"][1]["passed"] = False
        path.write_text(json.dumps(validation))
        result = run_baseline(self.provider, "healthy_001")
        self.assertEqual(result.finding, "Validation failed: required_fields_non_null")
        self.assertEqual(result.source, "validate")
        self.assertEqual(result.log_excerpt[0].number, 1)

    def test_rejects_unknown_names_and_limits(self):
        for run_id in ("missing", "../healthy_001", "healthy_001/pipeline.duckdb"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                self.provider.get_run_summary(run_id)
        with self.assertRaises(ValueError):
            self.provider.read_stage_log("healthy_001", "../transform")
        with self.assertRaises(ValueError):
            self.provider.compare_schema("healthy_001", "not_a_table")
        with self.assertRaises(ValueError):
            self.provider.profile_table("failed_001", "orders_daily")
        for count in (0, 21, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.provider.read_stage_log("healthy_001", "ingest", count)
        for count in (0, 6, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.provider.sample_rows("healthy_001", "raw_orders", count)

    def test_log_and_sample_are_bounded_and_reads_do_not_mutate(self):
        log_path = self.root / "healthy_001" / "logs" / "ingest.log"
        log_path.write_text("Z" * 500 + "\n" + "\n".join(f"line {i}" for i in range(30)) + "\n")
        db_path = self.root / "healthy_001" / "pipeline.duckdb"
        with duckdb.connect(str(db_path)) as db:
            db.execute("UPDATE raw_orders SET customer_id = ? WHERE order_id = 'O-001'", ["X" * 200])
        before_log = log_path.read_bytes()
        before_db = db_path.read_bytes()

        excerpt = self.provider.read_stage_log("healthy_001", "ingest", 2)
        sample = self.provider.sample_rows("healthy_001", "raw_orders", 2)
        self.provider.profile_table("healthy_001", "raw_orders")
        self.assertEqual([line.number for line in excerpt.lines], [1, 2])
        self.assertTrue(excerpt.truncated)
        self.assertLessEqual(len(excerpt.lines[0].text), MAX_LOG_LINE_CHARS)
        self.assertEqual(len(sample.rows), 2)
        self.assertTrue(sample.truncated)
        self.assertLessEqual(len(sample.rows[0]["customer_id"]), MAX_CELL_CHARS)
        self.assertEqual(log_path.read_bytes(), before_log)
        self.assertEqual(db_path.read_bytes(), before_db)

    def test_long_column_name_cannot_expand_profile_or_sample_output(self):
        db_path = self.root / "healthy_001" / "pipeline.duckdb"
        with duckdb.connect(str(db_path)) as db:
            db.execute(f'ALTER TABLE raw_orders RENAME COLUMN customer_id TO "{"X" * 81}"')
        with self.assertRaisesRegex(ValueError, "column name exceeds"):
            self.provider.profile_table("healthy_001", "raw_orders")
        with self.assertRaisesRegex(ValueError, "column name exceeds"):
            self.provider.sample_rows("healthy_001", "raw_orders")


if __name__ == "__main__":
    unittest.main()
