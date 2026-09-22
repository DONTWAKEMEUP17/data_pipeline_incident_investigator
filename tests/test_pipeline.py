import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

import duckdb

from incident_pipeline.pipeline import generate_runs


class PipelineTest(unittest.TestCase):
    def test_runs_and_saved_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            summaries = generate_runs(root)
            self.assertEqual([item["status"] for item in summaries], ["success", "failed"])

            healthy = root / "healthy_001"
            failed = root / "failed_001"
            for run in (healthy, failed):
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

    def test_regeneration_is_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            generate_runs(Path(first))
            generate_runs(Path(second))
            generate_runs(Path(first))
            for run_id in ("healthy_001", "failed_001"):
                for name in ("input.csv", "summary.json", "schemas.json", "validation.json"):
                    self.assertEqual((Path(first) / run_id / name).read_bytes(), (Path(second) / run_id / name).read_bytes())
                for stage in ("ingest", "transform", "validate"):
                    name = f"logs/{stage}.log"
                    self.assertEqual((Path(first) / run_id / name).read_bytes(), (Path(second) / run_id / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
