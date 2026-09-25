import json
import tempfile
import unittest
from pathlib import Path

from incident_pipeline.report import load_investigation, load_remediation, render_markdown


def _investigation():
    return {
        "report": {
            "run_id": "case_001",
            "root_cause_category": "join_reference",
            "explanation": "One customer key does not match the canonical reference representation.",
            "evidence_references": [
                {"reference_id": "tool-1", "claim": "Validation failed after transform succeeded."},
                {"reference_id": "tool-2", "claim": "The log identifies a case-sensitive lookup."},
            ],
            "proposed_human_action": "Confirm the key-normalization contract before changing the pipeline.",
            "uncertainty": "low",
            "changes_made": False,
        },
        "trace": [
            {
                "step": 1,
                "kind": "tool_result",
                "detail": {
                    "reference_id": "tool-1",
                    "tool": "get_run_summary",
                    "output": {
                        "status": "failed",
                        "stages": {
                            "transform": {"status": "success", "rows": 3},
                            "validate": {"status": "failed", "rows": 3},
                        },
                        "validation_checks": [{"name": "gate_17", "passed": False}],
                    },
                    "error": None,
                },
            },
            {
                "step": 2,
                "kind": "tool_result",
                "detail": {
                    "reference_id": "tool-2",
                    "tool": "read_stage_log",
                    "output": {
                        "stage": "validate",
                        "lines": [{"number": 2, "text": "One key failed exact matching."}],
                    },
                    "error": None,
                },
            },
            {"step": 3, "kind": "final_report", "detail": {"root_cause_category": "join_reference"}},
        ],
        "tool_call_count": 2,
        "model_step_count": 3,
        "model_api_calls": 3,
    }


def _remediation():
    return {
        "run_id": "case_001",
        "status": "verified_candidate",
        "candidate_name": "upper_trim",
        "candidate_change": "Apply UPPER(TRIM(customer_id)) before lookup.",
        "explanation": "The candidate resolved the unmatched key without a collision.",
        "assumptions": ["Customer IDs are case-insensitive."],
        "verification": {
            "row_count_before": 3,
            "row_count_after": 3,
            "unmatched_keys_before": 1,
            "unmatched_keys_after": 0,
            "changed_rows": 1,
            "null_keys_before": 0,
            "null_keys_after": 0,
            "source_normalization_collisions": 0,
            "reference_duplicate_keys": 0,
            "reference_noncanonical_keys": 0,
            "checks_passed": True,
            "blockers": [],
        },
        "change_preview": [{"order_id": "O-201", "before": "c-101", "after": "C-101"}],
        "proposed_human_action": "Approve the contract before implementation.",
        "approval_required": True,
        "sandbox_only": True,
        "changes_made": False,
    }


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_report_is_deterministic_grounded_and_clear_about_no_changes(self):
        investigation = _investigation()
        remediation = _remediation()

        first = render_markdown(investigation, remediation)
        second = render_markdown(investigation, remediation)

        self.assertEqual(first, second)
        self.assertIn("# Incident Report: case_001", first)
        self.assertIn("Human approval required", first)
        self.assertIn("**`tool-1` · `get_run_summary`**", first)
        self.assertIn("UPPER(TRIM(customer_id))", first)
        self.assertIn("| Unmatched customer keys | 1 | 0 |", first)
        self.assertIn("| `O-201` | `c-101` | `C-101` |", first)
        self.assertIn("Rendering made no model calls and changed no pipeline data", first)
        self.assertNotIn("Model API calls", first)
        self.assertNotIn("Tool execution trail", first)

    def test_debug_appendix_is_opt_in(self):
        rendered = render_markdown(_investigation(), include_debug=True)

        self.assertIn("## Technical appendix", rendered)
        self.assertIn("| Evidence tool calls | 2 |", rendered)
        self.assertIn("| Model steps | 3 |", rendered)
        self.assertIn("| Model API calls | 3 |", rendered)
        self.assertIn("### Tool execution trail", rendered)

    def test_unknown_evidence_reference_is_rejected(self):
        investigation = _investigation()
        investigation["report"]["evidence_references"][0]["reference_id"] = "tool-99"

        with self.assertRaisesRegex(ValueError, "unknown tool references"):
            render_markdown(investigation)

    def test_embedded_remediation_tool_result_is_rendered_without_a_second_file(self):
        investigation = _investigation()
        investigation["trace"].insert(-1, {
            "step": 3,
            "kind": "tool_result",
            "detail": {
                "reference_id": "tool-3",
                "tool": "verify_customer_key_normalization",
                "output": _remediation(),
                "error": None,
            },
        })
        investigation["report"]["evidence_references"].append({
            "reference_id": "tool-3",
            "claim": "The sandbox verified a collision-free normalization candidate.",
        })

        rendered = render_markdown(investigation)

        self.assertIn("| Candidate | `upper_trim` |", rendered)
        self.assertIn("`verify_customer_key_normalization`", rendered)

    def test_loads_one_case_from_evaluation_result(self):
        path = self.root / "evaluation.json"
        path.write_text(json.dumps({
            "cases": [
                {"run_id": "case_001", "agent": _investigation()},
                {"run_id": "case_002", "agent": _investigation()},
            ]
        }), encoding="utf-8")

        loaded = load_investigation(path, "case_001")

        self.assertEqual(loaded["report"]["run_id"], "case_001")
        with self.assertRaisesRegex(ValueError, "--run-id is required"):
            load_investigation(path)

    def test_mismatched_or_mutating_remediation_is_rejected(self):
        mismatch = self.root / "mismatch.json"
        value = _remediation()
        value["run_id"] = "another_run"
        mismatch.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            load_remediation(mismatch, "case_001")

        value = _remediation()
        value["changes_made"] = True
        with self.assertRaisesRegex(ValueError, "non-mutating"):
            render_markdown(_investigation(), value)


if __name__ == "__main__":
    unittest.main()
