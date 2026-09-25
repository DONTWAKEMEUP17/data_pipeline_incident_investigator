import json
import tempfile
import unittest
from pathlib import Path

from incident_pipeline.demo import DEMO_MARKER, create_demo
from incident_pipeline.feedback import FeedbackStore
from incident_pipeline.incident_web import IncidentRepository


class DemoTest(unittest.TestCase):
    def test_demo_is_grounded_isolated_and_repeatable(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "demo"
            first = create_demo(workspace)
            incidents = IncidentRepository(workspace / "incidents").scan().incidents
            event_ids = {incident.event_id for incident in incidents}
            categories = {incident.report["root_cause_category"] for incident in incidents}
            failed_tasks = {incident.manifest["task_id"] for incident in incidents}
            healthy_summary = json.loads(next(
                (workspace / "artifacts").glob("*/healthy_001/summary.json")
            ).read_text())

            self.assertTrue((workspace / DEMO_MARKER).is_file())
            self.assertEqual(first["pipeline_runs"]["healthy"], "success")
            self.assertEqual(healthy_summary["status"], "success")
            self.assertEqual(len(incidents), 2)
            self.assertEqual(categories, {"schema_drift", "data_quality"})
            self.assertEqual(failed_tasks, {"transform", "validate"})
            self.assertEqual(first["model_api_calls"], 0)
            self.assertFalse(first["pipeline_changes_from_investigator"])

            feedback = FeedbackStore(workspace / "feedback.sqlite3")
            feedback.save(next(iter(event_ids)), "useful")
            second = create_demo(workspace)

            repeated = IncidentRepository(workspace / "incidents").scan().incidents
            self.assertEqual({incident.event_id for incident in repeated}, event_ids)
            self.assertEqual(len(repeated), 2)
            self.assertEqual(len(FeedbackStore(workspace / "feedback.sqlite3").all()), 1)
            self.assertEqual(len(second["worker"]["processed"]), 2)

    def test_demo_refuses_nonempty_unmarked_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "not-a-demo"
            workspace.mkdir()
            (workspace / "keep.txt").write_text("user-owned\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not a demo workspace"):
                create_demo(workspace)

            self.assertEqual((workspace / "keep.txt").read_text(encoding="utf-8"), "user-owned\n")


if __name__ == "__main__":
    unittest.main()
