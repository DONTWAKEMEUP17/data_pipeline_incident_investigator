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
from incident_pipeline.rescore_v2 import rescore_document_v2
from incident_pipeline.scorer_v2 import SCORER_REVISION


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
        self.assertEqual(result["scorer_revision"], SCORER_REVISION)
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

        second["agent_revision"] = "revision-a"
        second["scorer_revision"] = "different-scorer"
        second_path.write_text(json.dumps(second), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incompatible scorer revision"):
            merge_results_v2([first_path, second_path])

    def test_rescorer_accepts_grounded_date_comparison_without_stale_keyword(self):
        case = next(case for case in CASES_V2 if case.run_id == "eval2_d007")
        record = {
            "run_id": case.run_id,
            "split": case.split,
            "family": case.family,
            "variant": case.variant,
            "expected_category": case.expected_category,
            "expected_behavior": case.expected_behavior,
            "expected_cause": case.expected_cause,
            "required_evidence_tools": list(case.required_evidence_tools),
            "baseline": {
                "prediction": "unknown", "tool_calls": 2, "latency_ms": 1.0,
                "output": {"finding": "Validation failed: gate_17", "source": "validate",
                           "log_excerpt": [{"number": 1, "text": "generic failure"}]},
            },
            "agent": {
                "prediction": "join_reference", "tool_calls": 2, "model_steps": 3,
                "latency_ms": 10.0, "model_api_calls": 3, "model_input_tokens": 100,
                "model_output_tokens": 50, "model_cost_usd": None,
                "report": {
                    "root_cause_category": "join_reference",
                    "explanation": (
                        "The reference snapshot is dated 2026-09-14 while the order batch is "
                        "dated 2026-09-21, and the current reference refresh is missing."
                    ),
                    "evidence_references": [{"reference_id": "tool-1", "claim": "Dates differ."}],
                    "proposed_human_action": "Refresh the reference.", "uncertainty": "low",
                    "changes_made": False,
                },
                "trace": [{
                    "step": 2, "kind": "tool_result", "detail": {
                        "reference_id": "tool-1", "tool": "read_stage_log", "error": None,
                        "output": {"lines": [
                            {"text": "Reference snapshot dated 2026-09-14."},
                            {"text": "Order batch dated 2026-09-21; current reference refresh is missing."},
                        ]},
                    },
                }],
            },
        }
        document = {
            "benchmark": "local_incidents_v2", "seed": 20260923,
            "adapter": "test", "agent_revision": "test-prompt", "cases": [record],
        }
        rescored = rescore_document_v2(document)
        agent = rescored["cases"][0]["agent"]
        self.assertEqual(rescored["scorer_revision"], SCORER_REVISION)
        self.assertTrue(agent["cause_identified"])
        self.assertTrue(agent["evidence_sufficient"])

    def test_rescorer_accepts_log_that_contains_complete_volume_evidence(self):
        source = evaluate_cases_v2(
            self.artifacts,
            [next(case for case in CASES_V2 if case.run_id == "eval2_d010")],
            DeterministicLearningAdapter,
            adapter_name="deterministic-learning-adapter",
            agent_revision="test",
        )
        record = source["cases"][0]
        record["agent"]["prediction"] = "freshness_volume"
        record["agent"]["report"]["root_cause_category"] = "freshness_volume"
        record["agent"]["report"]["explanation"] = "The batch has 1 row, below the minimum of 3 rows."
        record["agent"]["report"]["uncertainty"] = "low"
        record["agent"]["report"]["evidence_references"] = [
            {"reference_id": "tool-log", "claim": "The log states the threshold and observed count."}
        ]
        record["agent"]["trace"].append({
            "step": 3, "kind": "tool_result", "detail": {
                "reference_id": "tool-log", "tool": "read_stage_log", "error": None,
                "output": {"lines": [{"text": "Requires minimum 3 rows; current batch contains 1 row."}]},
            },
        })
        rescored = rescore_document_v2(source)
        self.assertTrue(rescored["cases"][0]["agent"]["evidence_sufficient"])


if __name__ == "__main__":
    unittest.main()
