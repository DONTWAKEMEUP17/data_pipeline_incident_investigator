import json
import tempfile
import unittest
from pathlib import Path

from incident_pipeline.incident_web import IncidentRepository, IncidentWebApp


class IncidentWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write_incident(
        self,
        event_id="a" * 24,
        run_id="manual__2026-09-25T02:34:21+00:00",
        explanation="The amount column changed.",
    ):
        directory = self.root / event_id
        directory.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "state": "complete",
            "event_id": event_id,
            "idempotency_key": event_id,
            "dag_id": "orders_daily_pipeline",
            "airflow_run_id": run_id,
            "task_id": "transform",
            "try_number": 1,
            "adapter": "deterministic-learning-adapter",
            "investigation_file": "investigation.json",
            "report_file": "report.md",
            "root_cause_category": "schema_drift",
            "model_api_calls": 0,
            "changes_made": False,
        }
        investigation = {
            "report": {
                "run_id": run_id,
                "root_cause_category": "schema_drift",
                "explanation": explanation,
                "evidence_references": [
                    {"reference_id": "tool-1", "claim": "Expected and observed schemas differ."}
                ],
                "proposed_human_action": "Review the upstream mapping.",
                "uncertainty": "low",
                "changes_made": False,
            },
            "trace": [
                {
                    "step": 1,
                    "kind": "tool_result",
                    "detail": {
                        "reference_id": "tool-1",
                        "tool": "compare_schema",
                        "output": {
                            "expected": [{"name": "amount", "type": "VARCHAR"}],
                            "observed": [{"name": "total_amount", "type": "VARCHAR"}],
                            "matches": False,
                        },
                        "error": None,
                    },
                }
            ],
            "model_api_calls": 0,
        }
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "investigation.json").write_text(json.dumps(investigation), encoding="utf-8")
        (directory / "report.md").write_text(f"# Incident Report: {run_id}\n", encoding="utf-8")
        return event_id

    def test_repository_lists_newest_first_and_omits_bad_artifact(self):
        older = self._write_incident("a" * 24, "manual__2026-09-25T01:00:00+00:00")
        newer = self._write_incident("b" * 24, "manual__2026-09-25T02:00:00+00:00")
        bad = self.root / ("c" * 24)
        bad.mkdir()
        (bad / "manifest.json").write_text("{}", encoding="utf-8")

        scan = IncidentRepository(self.root).scan()

        self.assertEqual([item.event_id for item in scan.incidents], [newer, older])
        self.assertEqual(scan.unreadable_count, 1)

    def test_pages_escape_artifact_text_and_reject_writes_and_traversal(self):
        event_id = self._write_incident(explanation="<script>alert('x')</script>")
        app = IncidentWebApp(IncidentRepository(self.root))

        listing = app.respond("GET", "/")
        detail = app.respond("GET", f"/incidents/{event_id}")
        markdown = app.respond("GET", f"/incidents/{event_id}/report.md")
        health = json.loads(app.respond("GET", "/healthz").body)

        self.assertEqual(listing.status, 200)
        self.assertIn(b"1 incidents", listing.body)
        self.assertEqual(detail.status, 200)
        self.assertIn(b"&lt;script&gt;", detail.body)
        self.assertNotIn(b"<script>alert", detail.body)
        self.assertIn(b"evidence-tool-1", detail.body)
        self.assertEqual(markdown.content_type, "text/markdown; charset=utf-8")
        self.assertEqual(health["incident_count"], 1)
        self.assertTrue(health["read_only"])
        self.assertEqual(app.respond("POST", "/").status, 405)
        self.assertEqual(app.respond("GET", "/incidents/%2e%2e%2fmanifest.json").status, 404)

if __name__ == "__main__":
    unittest.main()
