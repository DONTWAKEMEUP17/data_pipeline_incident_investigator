import tempfile
import unittest
from pathlib import Path

from incident_pipeline.feedback import FeedbackStore


class FeedbackStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FeedbackStore(Path(self.temp.name) / "feedback.sqlite3")

    def test_feedback_is_one_current_signal_per_incident(self):
        event_id = "a" * 24

        first = self.store.save(event_id, "useful", updated_at="2026-09-25T01:00:00+00:00")
        second = self.store.save(event_id, "uncertain", updated_at="2026-09-25T02:00:00+00:00")

        self.assertEqual(first.rating, "useful")
        self.assertEqual(second.rating, "uncertain")
        self.assertEqual(self.store.get(event_id), second)
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(self.store.counts(), {"incorrect": 0, "uncertain": 1, "useful": 0})

    def test_feedback_rejects_unknown_values_and_event_ids(self):
        for event_id, rating in (("../incident", "useful"), ("a" * 24, "great")):
            with self.subTest(event_id=event_id, rating=rating), self.assertRaises(ValueError):
                self.store.save(event_id, rating)


if __name__ == "__main__":
    unittest.main()
