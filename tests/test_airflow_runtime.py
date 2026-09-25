import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from incident_pipeline.airflow_runtime import (
    build_run_descriptor,
    descriptor_for_context,
    record_task_attempt,
)
from incident_pipeline.pipeline import (
    PipelineStageError,
    ingest_stage,
    transform_stage,
    validate_stage,
)


class AirflowRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_stage_functions_produce_healthy_run_and_are_retry_safe(self):
        ingest_stage("healthy_001", "orders_healthy.csv", self.root)
        transform_stage("healthy_001", self.root, raise_on_failure=True)
        first = validate_stage("healthy_001", self.root, raise_on_failure=True)
        transform_stage("healthy_001", self.root, raise_on_failure=True)
        second = validate_stage("healthy_001", self.root, raise_on_failure=True)

        self.assertEqual(first, second)
        self.assertEqual(second["status"], "success")

    def test_transform_failure_saves_evidence_before_raising(self):
        ingest_stage("failed_001", "orders_renamed_column.csv", self.root)

        with self.assertRaisesRegex(PipelineStageError, "schema mismatch") as raised:
            transform_stage("failed_001", self.root, raise_on_failure=True)

        summary = json.loads((self.root / "failed_001" / "summary.json").read_text())
        self.assertEqual(raised.exception.stage, "transform")
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["stages"]["transform"]["status"], "failed")
        self.assertEqual(summary["stages"]["validate"]["status"], "skipped")

    def test_duplicate_id_scenario_fails_validation_after_successful_transform(self):
        descriptor = build_run_descriptor(
            "orders_daily_pipeline",
            "manual__2026-09-24T01:30:00+00:00",
            "duplicate_id",
            self.root,
        )
        ingest_stage(descriptor["pipeline_run_id"], descriptor["input_name"], Path(descriptor["output_dir"]))
        transform_stage(descriptor["pipeline_run_id"], Path(descriptor["output_dir"]), raise_on_failure=True)

        with self.assertRaisesRegex(PipelineStageError, "validation failed") as raised:
            validate_stage(descriptor["pipeline_run_id"], Path(descriptor["output_dir"]), raise_on_failure=True)

        run_dir = Path(descriptor["output_dir"]) / descriptor["pipeline_run_id"]
        summary = json.loads((run_dir / "summary.json").read_text())
        validation = json.loads((run_dir / "validation.json").read_text())
        self.assertEqual(raised.exception.stage, "validate")
        self.assertEqual(summary["stages"]["transform"]["status"], "success")
        self.assertEqual(summary["stages"]["validate"]["status"], "failed")
        self.assertEqual(validation["status"], "failed")
        self.assertEqual(
            [check["name"] for check in validation["checks"] if not check["passed"]],
            ["order_ids_unique"],
        )

    def test_descriptor_is_allowlisted_and_airflow_identity_records_failed_attempt(self):
        descriptor = build_run_descriptor(
            "orders_daily_pipeline",
            "manual__2026-09-24T01:00:00+00:00",
            "schema_drift",
            self.root,
        )
        ingest_stage(
            descriptor["pipeline_run_id"],
            descriptor["input_name"],
            Path(descriptor["output_dir"]),
        )
        path = record_task_attempt(descriptor, "ingest", 1, "success")
        record_task_attempt(descriptor, "transform", 1, "failed")
        value = json.loads(path.read_text())

        self.assertNotIn(":", Path(descriptor["output_dir"]).name)
        self.assertEqual(value["dag_id"], "orders_daily_pipeline")
        self.assertEqual(value["airflow_run_id"], "manual__2026-09-24T01:00:00+00:00")
        self.assertEqual(
            value["task_attempts"],
            [
                {"state": "success", "task_id": "ingest", "try_number": 1},
                {"state": "failed", "task_id": "transform", "try_number": 1},
            ],
        )
        with self.assertRaisesRegex(ValueError, "unknown Airflow scenario"):
            build_run_descriptor("orders_daily_pipeline", "run", "arbitrary", self.root)

    def test_context_conf_overrides_default_scenario(self):
        context = {
            "dag": SimpleNamespace(dag_id="orders_daily_pipeline"),
            "dag_run": SimpleNamespace(conf={"scenario": "schema_drift"}),
            "params": {"scenario": "healthy"},
            "run_id": "manual__one",
        }

        descriptor = descriptor_for_context(context, self.root)

        self.assertEqual(descriptor["scenario"], "schema_drift")
        self.assertEqual(descriptor["pipeline_run_id"], "failed_001")


if __name__ == "__main__":
    unittest.main()
