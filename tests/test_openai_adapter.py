import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

from incident_pipeline.agent import RootCauseCategory, Uncertainty, investigate
from incident_pipeline.agent import ReportDraft as AgentReportDraft
from incident_pipeline.contracts import ReportDraft as ContractReportDraft
from incident_pipeline.evidence import LocalArtifactProvider
from incident_pipeline.openai_adapter import (
    SYSTEM_INSTRUCTIONS,
    DecisionEnvelope,
    FinalDecisionEnvelope,
    OpenAIModelAdapter,
)
from incident_pipeline.pipeline import generate_runs


class FakeResponses:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = kwargs["text_format"].model_validate(self.decisions.pop(0))
        usage = SimpleNamespace(input_tokens=120, output_tokens=30)
        return SimpleNamespace(output_parsed=parsed, usage=usage)


class FakeClient:
    def __init__(self, decisions):
        self.responses = FakeResponses(decisions)


class OpenAIAdapterTest(unittest.TestCase):
    def test_cli_and_adapter_share_one_contract_type(self):
        from incident_pipeline.openai_adapter import ReportDraft as AdapterReportDraft

        self.assertIs(AgentReportDraft, ContractReportDraft)
        self.assertIs(AdapterReportDraft, ContractReportDraft)
        self.assertEqual(ContractReportDraft.__module__, "incident_pipeline.contracts")

    def test_mocked_responses_api_drives_loop_without_network(self):
        client = FakeClient([
            {
                "kind": "tool", "tool": "get_run_summary", "table": None, "stage": None,
                "max_lines": None, "limit": None, "root_cause_category": None,
                "explanation": None, "evidence_references": [],
                "proposed_human_action": None, "uncertainty": None,
            },
        ])
        adapter = OpenAIModelAdapter("test-model", client=client, max_api_calls=2, max_output_tokens=300)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_runs(root)
            result = investigate(LocalArtifactProvider(root), adapter, "healthy_001", max_model_steps=2)

        self.assertEqual(result.report.root_cause_category, RootCauseCategory.UNKNOWN)
        self.assertEqual(result.report.uncertainty, Uncertainty.LOW)
        self.assertEqual(result.model_api_calls, 1)
        self.assertEqual(result.model_input_tokens, 120)
        self.assertEqual(result.model_output_tokens, 30)
        self.assertIsNone(result.model_cost_usd)
        for call in client.responses.calls:
            self.assertFalse(call["store"])
            self.assertEqual(call["max_output_tokens"], 300)
            self.assertEqual(call["reasoning"], {"effort": "low"})
        self.assertIs(client.responses.calls[0]["text_format"], DecisionEnvelope)
        self.assertEqual(result.trace[-1].kind, "guardrail_report")

    def test_sdk_strict_schema_uses_supported_flat_object(self):
        from openai.lib._pydantic import to_strict_json_schema

        schema = to_strict_json_schema(DecisionEnvelope)
        encoded = json.dumps(schema)
        self.assertEqual(schema["type"], "object")
        self.assertNotIn('"oneOf":', encoded)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        final_schema = to_strict_json_schema(FinalDecisionEnvelope)
        self.assertNotIn('"oneOf":', json.dumps(final_schema))

    def test_tool_parameter_schema_rejects_out_of_bounds_sample_limit(self):
        decision = {
            "kind": "tool", "tool": "sample_rows", "table": "raw_orders", "stage": None,
            "max_lines": None, "limit": 6, "root_cause_category": None,
            "explanation": None, "evidence_references": [],
            "proposed_human_action": None, "uncertainty": None,
        }
        with self.assertRaisesRegex(ValueError, "less than or equal to 5"):
            DecisionEnvelope.model_validate(decision)

    def test_adapter_requires_explicit_budgets(self):
        client = FakeClient([])
        with self.assertRaisesRegex(ValueError, "model"):
            OpenAIModelAdapter("", client=client)
        with self.assertRaisesRegex(ValueError, "max_api_calls"):
            OpenAIModelAdapter("test-model", client=client, max_api_calls=7)
        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            OpenAIModelAdapter("test-model", client=client, max_output_tokens=99)

    def test_prompt_defines_taxonomy_tool_policy_and_evidence_triggered_stopping(self):
        self.assertIn("stale reference snapshot", SYSTEM_INSTRUCTIONS)
        self.assertIn("stale reference data belongs to join_reference", SYSTEM_INSTRUCTIONS)
        self.assertIn("profile orders_daily before comparing schema", SYSTEM_INSTRUCTIONS)
        self.assertIn("unused budget alone is not a reason", SYSTEM_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
