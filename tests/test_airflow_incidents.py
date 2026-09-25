import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from incident_pipeline.airflow_incidents import process_failure_events
from incident_pipeline.airflow_runtime import (
    FAILURE_EVENTS_ENV,
    build_failure_event,
    build_run_descriptor,
    emit_failure_event,
    record_task_attempt,
    write_failure_event,
)
from incident_pipeline.evidence import AirflowEvidenceProvider
from incident_pipeline.pipeline import PipelineStageError, ingest_stage, transform_stage


class AirflowIncidentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.events = self.root / "events"
        self.incidents = self.root / "incidents"
        self.airflow_run_id = "manual__2026-09-24T02:00:00+00:00"
        self.context = {
            "dag": SimpleNamespace(dag_id="orders_daily_pipeline"),
            "run_id": self.airflow_run_id,
            "task_instance": SimpleNamespace(
                run_id=self.airflow_run_id,
                task_id="transform",
                try_number=1,
            ),
        }

    def _failed_run(self):
        descriptor = build_run_descriptor(
            "orders_daily_pipeline",
            self.airflow_run_id,
            "schema_drift",
            self.artifacts,
        )
        ingest_stage(
            descriptor["pipeline_run_id"],
            descriptor["input_name"],
            Path(descriptor["output_dir"]),
        )
        record_task_attempt(descriptor, "ingest", 1, "success")
        with self.assertRaises(PipelineStageError):
            transform_stage(
                descriptor["pipeline_run_id"],
                Path(descriptor["output_dir"]),
                raise_on_failure=True,
            )
        record_task_attempt(descriptor, "transform", 1, "failed")
        event = build_failure_event(self.context)
        write_failure_event(event, self.events)
        return descriptor, event

    def test_callback_event_is_minimal_and_idempotent(self):
        event = build_failure_event(self.context)
        first = write_failure_event(event, self.events)
        second = write_failure_event(event, self.events)

        self.assertEqual(first, second)
        self.assertEqual(len(list(self.events.glob("*.json"))), 1)
        self.assertEqual(
            set(event),
            {"schema_version", "event_id", "dag_id", "airflow_run_id", "task_id", "try_number"},
        )
        self.assertNotIn("error", json.dumps(event).lower())

        with patch.dict("os.environ", {FAILURE_EVENTS_ENV: str(self.events)}):
            emit_failure_event(self.context)
            emit_failure_event(self.context)
        self.assertEqual(len(list(self.events.glob("*.json"))), 1)

    def test_callback_handoff_error_never_replaces_task_error(self):
        emit_failure_event({})

    def test_airflow_provider_maps_identity_and_reads_without_mutation(self):
        descriptor, event = self._failed_run()
        run_dir = Path(descriptor["output_dir"]) / descriptor["pipeline_run_id"]
        before = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}

        provider = AirflowEvidenceProvider(event, self.artifacts)
        summary = provider.get_run_summary(self.airflow_run_id)
        comparison = provider.compare_schema(self.airflow_run_id, "raw_orders")
        profile = provider.profile_table(self.airflow_run_id, "raw_orders")

        after = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
        self.assertEqual(summary.run_id, self.airflow_run_id)
        self.assertEqual(summary.stages["transform"].status, "failed")
        self.assertFalse(comparison.matches)
        self.assertEqual(profile.row_count, 6)
        self.assertEqual(before, after)
        with self.assertRaisesRegex(ValueError, "unknown Airflow run ID"):
            provider.get_run_summary("another_run")

    def test_worker_creates_exactly_one_grounded_report(self):
        descriptor, event = self._failed_run()
        run_dir = Path(descriptor["output_dir"]) / descriptor["pipeline_run_id"]
        before = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}

        first = process_failure_events(self.events, self.artifacts, self.incidents)
        second = process_failure_events(self.events, self.artifacts, self.incidents)

        incident_dir = self.incidents / event["event_id"]
        investigation = json.loads((incident_dir / "investigation.json").read_text())
        manifest = json.loads((incident_dir / "manifest.json").read_text())
        report = (incident_dir / "report.md").read_text()
        after = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
        self.assertEqual(first["processed"], [event["event_id"]])
        self.assertEqual(second["skipped"], [event["event_id"]])
        self.assertEqual(len(list(incident_dir.glob("report.md"))), 1)
        self.assertEqual(investigation["report"]["root_cause_category"], "schema_drift")
        self.assertEqual(manifest["model_api_calls"], 0)
        self.assertFalse(manifest["changes_made"])
        self.assertIn("# Incident Report:", report)
        self.assertEqual(before, after)

    def test_bad_event_isolated_from_valid_incident(self):
        _, event = self._failed_run()
        (self.events / "bad.json").write_text("{}\n", encoding="utf-8")

        result = process_failure_events(self.events, self.artifacts, self.incidents)

        self.assertEqual(result["processed"], [event["event_id"]])
        self.assertEqual(result["failed"], ["bad"])
        self.assertTrue((self.incidents / event["event_id"] / "report.md").is_file())
        self.assertTrue((self.incidents / "failures" / "bad.json").is_file())


if __name__ == "__main__":
    unittest.main()
