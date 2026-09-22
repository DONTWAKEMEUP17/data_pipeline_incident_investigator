import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import duckdb

from incident_pipeline.pipeline import generate_run, generate_runs


class PipelineTest(unittest.TestCase):
    def test_runs_and_saved_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summaries = generate_runs(root)
            self.assertEqual([item["status"] for item in summaries], ["success", "failed", "failed", "failed"])

            healthy = root / "healthy_001"
            failed = root / "failed_001"
            duplicate_only = root / "failed_002"
            ambiguous = root / "ambiguous_001"
            for run in (healthy, failed, duplicate_only, ambiguous):
                for name in ("input.csv", "summary.json", "schemas.json", "validation.json", "pipeline.duckdb"):
                    self.assertTrue((run / name).exists(), name)
                for stage in ("ingest", "transform", "validate"):
                    self.assertTrue((run / "logs" / f"{stage}.log").exists())

            healthy_validation = json.loads((healthy / "validation.json").read_text())
            self.assertEqual(healthy_validation["status"], "passed")
            self.assertTrue(all(check["passed"] for check in healthy_validation["checks"]))
            contract = json.loads((Path(__file__).resolve().parent.parent / "pipeline_contract.json").read_text())
            healthy_schemas = json.loads((healthy / "schemas.json").read_text())
            self.assertEqual(healthy_schemas["orders_daily"], contract["tables"]["orders_daily"]["columns"])
            with duckdb.connect(str(healthy / "pipeline.duckdb"), read_only=True) as db:
                self.assertEqual(db.execute("SELECT COUNT(*), SUM(amount) FROM orders_daily").fetchone(), (3, Decimal("49.75")))

            failed_summary = json.loads((failed / "summary.json").read_text())
            self.assertEqual(failed_summary["stages"]["ingest"]["status"], "success")
            self.assertEqual(failed_summary["stages"]["transform"]["status"], "failed")
            self.assertEqual(failed_summary["stages"]["validate"]["status"], "skipped")
            self.assertEqual(json.loads((failed / "validation.json").read_text())["status"], "skipped")
            observed = json.loads((failed / "schemas.json").read_text())["raw_orders"]
            self.assertEqual([column["name"] for column in observed], ["order_id", "customer_id", "total_amount", "order_date"])
            with duckdb.connect(str(failed / "pipeline.duckdb"), read_only=True) as db:
                self.assertEqual(db.execute("SHOW TABLES").fetchall(), [("raw_orders",)])

            self.assertEqual(failed_summary["stages"]["ingest"]["rows"], 6)
            duplicate_summary = json.loads((duplicate_only / "summary.json").read_text())
            duplicate_validation = json.loads((duplicate_only / "validation.json").read_text())
            self.assertEqual(duplicate_summary["stages"]["transform"]["status"], "success")
            self.assertEqual(duplicate_summary["stages"]["validate"]["status"], "failed")
            self.assertEqual(duplicate_validation["status"], "failed")
            self.assertEqual(
                [check for check in duplicate_validation["checks"] if not check["passed"]],
                [{"name": "order_ids_unique", "observed": 1, "passed": False}],
            )
            self.assertEqual((failed / "input.csv").read_text().replace("total_amount", "amount", 1), (duplicate_only / "input.csv").read_text())
            ambiguous_validation = json.loads((ambiguous / "validation.json").read_text())
            self.assertEqual(ambiguous_validation["status"], "failed")
            self.assertEqual(
                [check for check in ambiguous_validation["checks"] if not check["passed"]],
                [{"name": "row_count_positive", "observed": 0, "passed": False}],
            )

    def test_regeneration_is_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            generate_runs(Path(first))
            generate_runs(Path(second))
            generate_runs(Path(first))
            for run_id in ("healthy_001", "failed_001", "failed_002", "ambiguous_001"):
                for name in ("input.csv", "summary.json", "schemas.json", "validation.json"):
                    self.assertEqual((Path(first) / run_id / name).read_bytes(), (Path(second) / run_id / name).read_bytes())
                for stage in ("ingest", "transform", "validate"):
                    name = f"logs/{stage}.log"
                    self.assertEqual((Path(first) / run_id / name).read_bytes(), (Path(second) / run_id / name).read_bytes())

    def test_repeated_header_is_rejected_before_writing_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture_dir = Path(temp) / "fixtures"
            fixture_dir.mkdir()
            (fixture_dir / "orders_duplicate_id.csv").write_text(
                "order_id,customer_id,amount,order_date\n"
                "O-001,C-101,12.50,2026-01-15\n"
                "order_id,customer_id,amount,order_date\n"
            )
            output = Path(temp) / "artifacts"
            with patch("incident_pipeline.pipeline.FIXTURE_DIR", fixture_dir):
                with self.assertRaisesRegex(ValueError, "repeated header"):
                    generate_run("failed_002", "orders_duplicate_id.csv", output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
