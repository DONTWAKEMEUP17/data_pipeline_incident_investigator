import json
import re
import tempfile
import unittest
from itertools import count
from pathlib import Path

from incident_pipeline.agent import DeterministicLearningAdapter
from incident_pipeline.evaluate_v2 import evaluate_cases_v2
from incident_pipeline.evaluation_cases_v2 import CASES_V2, generate_evaluation_cases_v2
from incident_pipeline.merge_evaluations_v2 import merge_results_v2


class EvaluationV2Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.manifest_path = self.root / "cases.json"
        self.manifest = generate_evaluation_cases_v2(self.artifacts, self.manifest_path)

    def test_case_set_is_versioned_opaque_and_split_before_evaluation(self):
        self.assertEqual(self.manifest["benchmark"], "local_incidents_v2")
        self.assertEqual(len(CASES_V2), 18)
        self.assertEqual(sum(case.split == "development" for case in CASES_V2), 12)
        self.assertEqual(sum(case.split == "heldout" for case in CASES_V2), 6)
        self.assertTrue(all(re.fullmatch(r"eval2_[dh]\d{3}", case.run_id) for case in CASES_V2))
        self.assertTrue(self.manifest["labels_are_not_agent_input"])
        families = {case.family for case in CASES_V2}
        self.assertEqual(
            families,
            {"schema_drift", "data_quality", "join_reference", "freshness_volume", "healthy", "ambiguous"},
        )
        for family in families:
            self.assertTrue(any(case.family == family and case.split == "heldout" for case in CASES_V2))

    def test_generic_check_name_is_reused_across_cause_families(self):
        gate_families = set()
        for case in CASES_V2:
            validation = json.loads(
                (self.artifacts / case.run_id / "validation.json").read_text(encoding="utf-8")
            )
            names = {check["name"] for check in validation["checks"]}
            self.assertTrue(all(re.fullmatch(r"gate_\d+", name) for name in names))
            if "gate_17" in names:
                gate_families.add(case.family)
        self.assertTrue({"join_reference", "freshness_volume", "ambiguous"}.issubset(gate_families))

    def test_generation_is_reproducible_and_scoring_labels_are_not_artifacts(self):
        second = self.root / "second"
        generate_evaluation_cases_v2(second)
        for case in CASES_V2:
            run_dir = self.artifacts / case.run_id
            for relative in (
                "input.csv", "summary.json", "schemas.json", "validation.json",
                "logs/ingest.log", "logs/transform.log", "logs/validate.log",
            ):
                first = (run_dir / relative).read_bytes()
                self.assertEqual(first, (second / case.run_id / relative).read_bytes())
                self.assertNotIn(b"expected_category", first)
                self.assertNotIn(b"expected_cause", first)
                self.assertNotIn(case.variant.encode(), first)

    def test_development_run_exposes_v1_shortcut_failure(self):
        development = tuple(case for case in CASES_V2 if case.split == "development")
        ticks = count()
        result = evaluate_cases_v2(
            self.artifacts,
            development,
            DeterministicLearningAdapter,
            adapter_name="deterministic-learning-adapter",
            agent_revision="deterministic-learning-adapter-v1",
            clock=lambda: next(ticks) / 1_000,
        )
        self.assertEqual(result["benchmark"], "local_incidents_v2")
        self.assertEqual(result["case_count"], 12)
        self.assertTrue(all(row["split"] == "development" for row in result["cases"]))
        overall = result["metrics"]["overall"]
        self.assertLess(overall["baseline"]["category_accuracy"], 0.5)
        self.assertEqual(overall["baseline"]["cause_accuracy"], 0.0)
        self.assertEqual(overall["baseline"]["evidence_validity_rate"], 1.0)
        self.assertGreater(overall["agent"]["cause_accuracy"], overall["baseline"]["cause_accuracy"])
        self.assertEqual(overall["agent"]["evidence_validity_rate"], 1.0)
        self.assertEqual(overall["agent"]["average_latency_ms"], 1.0)

    def test_merge_rejects_mixed_revisions_and_preserves_v2_order(self):
        ticks = count()
        first = evaluate_cases_v2(
            self.artifacts,
            CASES_V2[:2],
            DeterministicLearningAdapter,
            adapter_name="deterministic-learning-adapter",
            agent_revision="revision-a",
            clock=lambda: next(ticks) / 1_000,
        )
        second = evaluate_cases_v2(
            self.artifacts,
            CASES_V2[2:4],
            DeterministicLearningAdapter,
            adapter_name="deterministic-learning-adapter",
            agent_revision="revision-a",
            clock=lambda: next(ticks) / 1_000,
        )
        first_path = self.root / "first.json"
        second_path = self.root / "second.json"
        first_path.write_text(json.dumps(first), encoding="utf-8")
        second_path.write_text(json.dumps(second), encoding="utf-8")

        merged = merge_results_v2([first_path, second_path])
        self.assertEqual([row["run_id"] for row in merged["cases"]], [case.run_id for case in CASES_V2[:4]])
        self.assertEqual(merged["case_count"], 4)
        self.assertEqual(len(merged["source_results"]), 2)

        second["agent_revision"] = "revision-b"
        second_path.write_text(json.dumps(second), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incompatible agent revision"):
            merge_results_v2([first_path, second_path])


if __name__ == "__main__":
    unittest.main()
