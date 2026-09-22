import tempfile
import unittest
from pathlib import Path

from incident_pipeline.agent import (
    DeterministicLearningAdapter,
    EvidenceReference,
    ReportDraft,
    RootCauseCategory,
    ToolRequest,
    Uncertainty,
    investigate,
)
from incident_pipeline.evidence import LocalArtifactProvider
from incident_pipeline.pipeline import generate_runs


class ScriptedModel:
    name = "mock-model"
    cost_usd = 0.0

    def __init__(self, decisions):
        self.decisions = list(decisions)

    def next_decision(self, run_id, observations):
        return self.decisions.pop(0)


class RepeatingModel:
    name = "mock-repeating-model"
    cost_usd = 0.0

    def next_decision(self, run_id, observations):
        return ToolRequest("get_run_summary", {})


class FailingModel:
    name = "mock-failing-model"
    cost_usd = None

    def next_decision(self, run_id, observations):
        raise RuntimeError("simulated provider failure")


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        generate_runs(self.root)
        self.provider = LocalArtifactProvider(self.root)

    def test_local_adapter_diagnoses_schema_drift_with_dynamic_trace(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = investigate(self.provider, DeterministicLearningAdapter(), "failed_001")
        after = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        self.assertEqual(result.report.root_cause_category, RootCauseCategory.SCHEMA_DRIFT)
        self.assertIn("additional issue", result.report.explanation)
        self.assertEqual(result.tool_call_count, 3)
        requested = [event.detail["tool"] for event in result.trace if event.kind == "tool_request"]
        self.assertEqual(requested, ["get_run_summary", "compare_schema", "profile_table"])
        tool_refs = {event.detail["reference_id"] for event in result.trace if event.kind == "tool_result"}
        self.assertTrue({item.reference_id for item in result.report.evidence_references}.issubset(tool_refs))
        self.assertFalse(result.report.changes_made)
        self.assertEqual(before, after)

    def test_local_adapter_diagnoses_duplicate_id(self):
        result = investigate(self.provider, DeterministicLearningAdapter(), "failed_002")
        self.assertEqual(result.report.root_cause_category, RootCauseCategory.DATA_QUALITY)
        self.assertEqual(result.report.uncertainty, Uncertainty.LOW)
        self.assertEqual(result.tool_call_count, 2)

    def test_local_adapter_abstains_on_ambiguous_empty_batch(self):
        result = investigate(self.provider, DeterministicLearningAdapter(), "ambiguous_001")
        self.assertEqual(result.report.root_cause_category, RootCauseCategory.UNKNOWN)
        self.assertEqual(result.report.uncertainty, Uncertainty.HIGH)
        self.assertIn("cannot distinguish", result.report.explanation)
        self.assertEqual(result.tool_call_count, 2)

    def test_mocked_model_response_and_evidence_validation(self):
        valid = ScriptedModel([
            ToolRequest("get_run_summary", {}),
            ReportDraft(
                RootCauseCategory.UNKNOWN,
                "The mocked response does not assign a cause.",
                (EvidenceReference("tool-1", "The summary is the only inspected evidence."),),
                "Review the summary.",
                Uncertainty.HIGH,
            ),
        ])
        result = investigate(self.provider, valid, "healthy_001")
        self.assertEqual(result.report.evidence_references[0].reference_id, "tool-1")

        invalid = ScriptedModel([
            ToolRequest("get_run_summary", {}),
            ReportDraft(
                RootCauseCategory.SCHEMA_DRIFT,
                "This draft cites evidence that was never returned.",
                (EvidenceReference("tool-99", "Invented evidence."),),
                "Review it.",
                Uncertainty.LOW,
            ),
        ])
        result = investigate(self.provider, invalid, "healthy_001")
        self.assertEqual(result.report.root_cause_category, RootCauseCategory.UNKNOWN)
        self.assertEqual(result.report.uncertainty, Uncertainty.HIGH)
        self.assertEqual(result.trace[-1].kind, "validation_error")

    def test_budget_exhaustion_returns_partial_abstention(self):
        result = investigate(self.provider, RepeatingModel(), "failed_001", max_tool_calls=1)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.report.root_cause_category, RootCauseCategory.UNKNOWN)
        self.assertEqual(result.trace[-1].kind, "budget_exhausted")

    def test_last_model_step_does_not_execute_another_tool(self):
        result = investigate(self.provider, RepeatingModel(), "failed_001", max_model_steps=1)
        self.assertEqual(result.tool_call_count, 0)
        self.assertEqual(result.report.root_cause_category, RootCauseCategory.UNKNOWN)
        self.assertEqual(result.trace[-1].detail["tool_not_executed"], "get_run_summary")

    def test_model_error_returns_abstention(self):
        result = investigate(self.provider, FailingModel(), "failed_001")
        self.assertEqual(result.report.root_cause_category, RootCauseCategory.UNKNOWN)
        self.assertEqual(result.trace[-1].kind, "model_error")
        self.assertEqual(result.report.evidence_references, ())

    def test_report_schema_rejects_invalid_values_and_execution_claim(self):
        with self.assertRaisesRegex(ValueError, "root_cause_category"):
            ReportDraft("not-a-category", "Explanation", (EvidenceReference("tool-1", "Claim"),),
                        "Review it.", Uncertainty.HIGH)
        with self.assertRaisesRegex(ValueError, "must not claim"):
            ReportDraft(RootCauseCategory.UNKNOWN, "I fixed the pipeline.",
                        (EvidenceReference("tool-1", "Claim"),), "Review it.", Uncertainty.HIGH)


if __name__ == "__main__":
    unittest.main()
