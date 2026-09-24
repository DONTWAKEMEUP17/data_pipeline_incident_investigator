import csv
import tempfile
import unittest
from pathlib import Path

import duckdb

from incident_pipeline.agent import investigate
from incident_pipeline.contracts import (
    EvidenceReference,
    ReportDraft,
    RootCauseCategory,
    ToolRequest,
    Uncertainty,
)
from incident_pipeline.evaluation_cases_v2 import generate_evaluation_cases_v2
from incident_pipeline.evidence import LocalArtifactProvider
from incident_pipeline.pipeline import FIXTURE_DIR
from incident_pipeline.remediation import propose_customer_key_remediation


class ScriptedRemediationModel:
    name = "scripted-remediation-model"
    cost_usd = 0.0

    def __init__(self, decisions):
        self.decisions = list(decisions)

    def next_decision(self, run_id, observations):
        return self.decisions.pop(0)


class RemediationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = self.root / "customer_reference.csv"
        with self.reference.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(["customer_id"])
            writer.writerows([["C-101"], ["C-102"], ["C-103"]])

    def _run_dir(self, rows):
        run_dir = self.root / "run"
        run_dir.mkdir(exist_ok=True)
        database = run_dir / "pipeline.duckdb"
        connection = duckdb.connect(str(database))
        try:
            connection.execute("CREATE TABLE orders_daily(order_id VARCHAR, customer_id VARCHAR)")
            connection.executemany("INSERT INTO orders_daily VALUES (?, ?)", rows)
        finally:
            connection.close()
        (run_dir / "input.csv").write_text("synthetic snapshot\n", encoding="utf-8")
        return run_dir

    def test_verified_upper_trim_candidate_does_not_modify_run_artifacts(self):
        run_dir = self._run_dir([
            ("O-201", "c-101"),
            ("O-202", "C-102"),
            ("O-203", "C-103"),
        ])
        before = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}

        proposal = propose_customer_key_remediation("eval2_d008", run_dir, self.reference)

        after = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(proposal.status, "verified_candidate")
        self.assertEqual(proposal.root_cause_category, "join_reference")
        self.assertEqual(proposal.candidate_name, "upper_trim")
        self.assertTrue(proposal.verification.checks_passed)
        self.assertEqual(proposal.verification.unmatched_keys_before, 1)
        self.assertEqual(proposal.verification.unmatched_keys_after, 0)
        self.assertEqual(proposal.verification.row_count_before, proposal.verification.row_count_after)
        self.assertEqual(proposal.verification.source_normalization_collisions, 0)
        self.assertEqual(proposal.change_preview[0].before, "c-101")
        self.assertEqual(proposal.change_preview[0].after, "C-101")
        self.assertTrue(proposal.approval_required)
        self.assertTrue(proposal.sandbox_only)
        self.assertFalse(proposal.changes_made)

    def test_candidate_is_rejected_when_distinct_source_keys_collapse(self):
        run_dir = self._run_dir([
            ("O-201", "c-101"),
            ("O-202", "C-101"),
        ])
        proposal = propose_customer_key_remediation("collision", run_dir, self.reference)

        self.assertEqual(proposal.status, "no_safe_candidate")
        self.assertFalse(proposal.verification.checks_passed)
        self.assertEqual(proposal.verification.source_normalization_collisions, 1)
        self.assertTrue(any("collapse" in blocker for blocker in proposal.verification.blockers))
        self.assertFalse(proposal.changes_made)

    def test_candidate_is_rejected_when_normalization_does_not_resolve_lookup(self):
        run_dir = self._run_dir([("O-201", "X-999")])
        proposal = propose_customer_key_remediation("unresolved", run_dir, self.reference)

        self.assertEqual(proposal.status, "no_safe_candidate")
        self.assertGreater(proposal.verification.unmatched_keys_after, 0)
        self.assertTrue(any("unmatched" in blocker for blocker in proposal.verification.blockers))

    def test_reference_schema_is_bounded(self):
        bad_reference = self.root / "bad.csv"
        bad_reference.write_text("customer_id,status\nC-101,active\n", encoding="utf-8")
        run_dir = self._run_dir([("O-201", "c-101")])
        with self.assertRaisesRegex(ValueError, "exactly one customer_id"):
            propose_customer_key_remediation("bad-reference", run_dir, bad_reference)

    def test_agent_can_cite_verified_sandbox_candidate_without_changing_artifacts(self):
        artifacts = self.root / "artifacts"
        generate_evaluation_cases_v2(artifacts, self.root / "cases.json")
        provider = LocalArtifactProvider(
            artifacts,
            allowed_run_ids=("eval2_d008",),
            customer_reference_csv=FIXTURE_DIR / "customer_reference.csv",
        )
        run_dir = artifacts / "eval2_d008"
        before = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
        model = ScriptedRemediationModel([
            ToolRequest("get_run_summary", {}),
            ToolRequest("read_stage_log", {"stage": "validate", "max_lines": 5}),
            ToolRequest("sample_rows", {"table": "orders_daily", "limit": 3}),
            ToolRequest("verify_customer_key_normalization", {}),
            ReportDraft(
                RootCauseCategory.JOIN_REFERENCE,
                "A casing-only customer-key mismatch has a sandbox-verified normalization candidate.",
                (EvidenceReference("tool-4", "The candidate resolved the mismatch without collisions."),),
                "Confirm the key contract before approving a pipeline change.",
                Uncertainty.LOW,
            ),
        ])

        result = investigate(provider, model, "eval2_d008", max_model_steps=5)

        after = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
        remediation_result = next(
            event.detail["output"]
            for event in result.trace
            if event.kind == "tool_result" and event.detail["tool"] == "verify_customer_key_normalization"
        )
        self.assertEqual(before, after)
        self.assertEqual(result.report.root_cause_category, RootCauseCategory.JOIN_REFERENCE)
        self.assertEqual(result.tool_call_count, 4)
        self.assertEqual(remediation_result["status"], "verified_candidate")
        self.assertTrue(remediation_result["approval_required"])
        self.assertTrue(remediation_result["sandbox_only"])
        self.assertFalse(remediation_result["changes_made"])

    def test_agent_rejects_remediation_tool_before_diagnostic_evidence(self):
        self._run_dir([("O-201", "c-101")])
        provider = LocalArtifactProvider(
            self.root,
            allowed_run_ids=("run",),
            customer_reference_csv=self.reference,
        )
        model = ScriptedRemediationModel([
            ToolRequest("verify_customer_key_normalization", {}),
            ReportDraft(
                RootCauseCategory.UNKNOWN,
                "Verification was rejected because diagnostic evidence was not collected first.",
                (EvidenceReference("tool-1", "The verifier returned a precondition error."),),
                "Collect the required evidence before proposing remediation.",
                Uncertainty.HIGH,
            ),
        ])

        result = investigate(provider, model, "run", max_model_steps=2)

        tool_result = next(event for event in result.trace if event.kind == "tool_result")
        self.assertIsNone(tool_result.detail["output"])
        self.assertIn("requires a run summary", tool_result.detail["error"])


if __name__ == "__main__":
    unittest.main()
