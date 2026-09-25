import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from incident_pipeline.feedback import FeedbackStore
from incident_pipeline.incident_web import IncidentRepository, IncidentWebApp


class IncidentWebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feedback = FeedbackStore(self.root / "feedback.sqlite3")

    def _app(self):
        return IncidentWebApp(IncidentRepository(self.root), self.feedback, "test-csrf-token")

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
        app = self._app()

        listing = app.respond("GET", "/")
        detail = app.respond("GET", f"/incidents/{event_id}")
        markdown = app.respond("GET", f"/incidents/{event_id}/report.md")
        health = json.loads(app.respond("GET", "/healthz").body)

        self.assertEqual(listing.status, 200)
        self.assertIn(b"1 shown", listing.body)
        self.assertIn(b"1 cause category", listing.body)
        self.assertIn(b'class="cause-tag">Schema drift', listing.body)
        self.assertIn(b'class="review-pill awaiting"', listing.body)
        self.assertIn(b"Awaiting review", listing.body)
        self.assertEqual(detail.status, 200)
        self.assertIn(b"&lt;script&gt;", detail.body)
        self.assertNotIn(b"<script>alert", detail.body)
        self.assertIn(b"evidence-tool-1", detail.body)
        self.assertEqual(markdown.content_type, "text/markdown; charset=utf-8")
        self.assertEqual(health["incident_count"], 1)
        self.assertTrue(health["pipeline_data_read_only"])
        self.assertTrue(health["feedback_writable"])
        self.assertEqual(app.respond("POST", "/").status, 405)
        self.assertEqual(app.respond("GET", "/incidents/%2e%2e%2fmanifest.json").status, 404)

    def test_review_queue_filters_useful_from_items_needing_review(self):
        awaiting = self._write_incident("a" * 24, "manual__2026-09-25T01:00:00+00:00")
        useful = self._write_incident("b" * 24, "manual__2026-09-25T02:00:00+00:00")
        incorrect = self._write_incident("c" * 24, "manual__2026-09-25T03:00:00+00:00")
        uncertain = self._write_incident("d" * 24, "manual__2026-09-25T04:00:00+00:00")
        self.feedback.save(useful, "useful")
        self.feedback.save(incorrect, "incorrect")
        self.feedback.save(uncertain, "uncertain")
        app = self._app()

        needs_review = app.respond("GET", "/?review=needs_review")
        awaiting_only = app.respond("GET", "/?review=awaiting")
        incorrect_only = app.respond("GET", "/?review=incorrect")
        uncertain_only = app.respond("GET", "/?review=uncertain")
        useful_only = app.respond("GET", "/?review=useful")

        self.assertEqual(needs_review.status, 200)
        self.assertIn(b"Needs review<span class=\"filter-count\">3", needs_review.body)
        for event_id in (awaiting, incorrect, uncertain):
            self.assertIn(f'/incidents/{event_id}"'.encode(), needs_review.body)
        self.assertNotIn(f'/incidents/{useful}"'.encode(), needs_review.body)
        self.assertIn(f'/incidents/{awaiting}"'.encode(), awaiting_only.body)
        self.assertNotIn(f'/incidents/{incorrect}"'.encode(), awaiting_only.body)
        self.assertIn(f'/incidents/{incorrect}"'.encode(), incorrect_only.body)
        self.assertIn(f'/incidents/{uncertain}"'.encode(), uncertain_only.body)
        self.assertIn(f'/incidents/{useful}"'.encode(), useful_only.body)
        self.assertEqual(app.respond("GET", "/?review=wrong").status, 400)
        self.assertEqual(app.respond("GET", "/?review=useful&review=incorrect").status, 400)

    def test_feedback_post_is_csrf_checked_upserted_and_separate_from_incident_files(self):
        event_id = self._write_incident()
        app = self._app()
        incident_dir = self.root / event_id
        before = {path.name: path.read_bytes() for path in incident_dir.iterdir()}

        response = app.respond(
            "POST",
            f"/incidents/{event_id}/feedback",
            body=urlencode({"csrf_token": "test-csrf-token", "rating": "useful"}).encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(response.status, 303)
        self.assertEqual(dict(response.headers)["Location"], f"/incidents/{event_id}")
        self.assertEqual(self.feedback.get(event_id).rating, "useful")
        detail_after_review = app.respond("GET", f"/incidents/{event_id}").body
        listing_after_review = app.respond("GET", "/").body
        self.assertIn(b"Your review: Useful", detail_after_review)
        self.assertIn(b"It does not mean the pipeline has been fixed", detail_after_review)
        self.assertIn(b'class="review-pill useful"', listing_after_review)
        self.assertIn(b"Report useful", listing_after_review)

        updated = app.respond(
            "POST",
            f"/incidents/{event_id}/feedback",
            body=urlencode({"csrf_token": "test-csrf-token", "rating": "incorrect"}).encode(),
            headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
        )
        rejected = app.respond(
            "POST",
            f"/incidents/{event_id}/feedback",
            body=urlencode({"csrf_token": "wrong", "rating": "useful"}).encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        after = {path.name: path.read_bytes() for path in incident_dir.iterdir()}

        self.assertEqual(updated.status, 303)
        self.assertEqual(rejected.status, 403)
        self.assertEqual(self.feedback.get(event_id).rating, "incorrect")
        incorrect_listing = app.respond("GET", "/").body
        self.assertIn(b'class="review-pill incorrect"', incorrect_listing)
        self.assertIn(b"Diagnosis incorrect", incorrect_listing)

        self.feedback.save(event_id, "uncertain")
        uncertain_listing = app.respond("GET", "/").body
        self.assertIn(b'class="review-pill uncertain"', uncertain_listing)
        self.assertIn(b"Uncertain", uncertain_listing)
        self.assertEqual(len(self.feedback.all()), 1)
        self.assertEqual(before, after)

if __name__ == "__main__":
    unittest.main()
