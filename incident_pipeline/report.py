"""Render a validated investigation result as a concise Markdown incident report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT_CAUSE_LABELS = {
    "schema_drift": "Schema drift",
    "data_quality": "Data quality",
    "join_reference": "Join / reference",
    "freshness_volume": "Freshness / volume",
    "unknown": "Unknown / abstained",
}
UNCERTAINTY_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _items(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _markdown(value: Any) -> str:
    """Keep artifact text readable without letting it create Markdown structure."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    for character in ("\\", "`", "*", "[", "]", "<", ">", "|"):
        text = text.replace(character, "\\" + character)
    return " ".join(text.split())


def load_investigation(path: Path, run_id: str | None = None) -> dict[str, Any]:
    """Load either direct agent output or one case from a saved evaluation result."""
    raw = _object(json.loads(path.read_text(encoding="utf-8")), "investigation input")
    if "report" in raw and "trace" in raw:
        investigation = raw
    elif "cases" in raw:
        if not run_id:
            raise ValueError("--run-id is required for an evaluation result")
        matches = [
            _object(case, "evaluation case")
            for case in _items(raw["cases"], "evaluation cases")
            if isinstance(case, dict) and case.get("run_id") == run_id
        ]
        if len(matches) != 1:
            raise ValueError(f"evaluation result must contain exactly one case for run_id {run_id!r}")
        investigation = _object(matches[0].get("agent"), "evaluation agent result")
    else:
        raise ValueError("input must be direct agent output or an evaluation result")

    report = _object(investigation.get("report"), "report")
    observed_run_id = _text(report.get("run_id"), "report.run_id")
    if run_id is not None and observed_run_id != run_id:
        raise ValueError(f"requested run_id {run_id!r} does not match report run_id {observed_run_id!r}")
    _items(investigation.get("trace"), "trace")
    return investigation


def load_remediation(path: Path, run_id: str) -> dict[str, Any]:
    proposal = _object(json.loads(path.read_text(encoding="utf-8")), "remediation proposal")
    proposal_run_id = _text(proposal.get("run_id"), "remediation.run_id")
    if proposal_run_id != run_id:
        raise ValueError(
            f"remediation run_id {proposal_run_id!r} does not match investigation run_id {run_id!r}"
        )
    if proposal.get("changes_made") is not False or proposal.get("sandbox_only") is not True:
        raise ValueError("remediation proposal must be sandbox-only and must not claim changes were made")
    return proposal


def _tool_results(trace: list[Any]) -> tuple[dict[str, dict[str, Any]], list[tuple[int, str, str]]]:
    results: dict[str, dict[str, Any]] = {}
    trail: list[tuple[int, str, str]] = []
    for raw_event in trace:
        event = _object(raw_event, "trace event")
        detail = _object(event.get("detail"), "trace event detail")
        if event.get("kind") == "tool_result":
            reference_id = _text(detail.get("reference_id"), "tool result reference_id")
            if reference_id in results:
                raise ValueError(f"duplicate tool reference: {reference_id}")
            results[reference_id] = detail
            trail.append((int(event.get("step", 0)), _text(detail.get("tool"), "tool result name"),
                          "error" if detail.get("error") else "ok"))
    return results, trail


def _evidence_summary(tool: str, output: Any, error: Any) -> str:
    if error:
        return f"Tool error: {_markdown(error)}"
    if not isinstance(output, dict):
        return "No structured output was returned."
    if tool == "get_run_summary":
        stages = output.get("stages", {})
        stage_text = ", ".join(
            f"{name}={details.get('status')}"
            + (f" ({details.get('rows')} rows)" if details.get("rows") is not None else "")
            for name, details in stages.items()
            if isinstance(details, dict)
        )
        failed = [
            str(check.get("name"))
            for check in output.get("validation_checks", [])
            if isinstance(check, dict) and check.get("passed") is False
        ]
        suffix = f" Failed checks: {', '.join(failed)}." if failed else ""
        return _markdown(f"Run status={output.get('status')}; {stage_text}.{suffix}")
    if tool == "read_stage_log":
        lines = output.get("lines", [])
        rendered = " ".join(
            f"L{line.get('number')}: {line.get('text')}" for line in lines if isinstance(line, dict)
        )
        return _markdown(rendered or "The bounded log excerpt was empty.")
    if tool == "compare_schema":
        expected = [column.get("name") for column in output.get("expected", []) if isinstance(column, dict)]
        observed = [column.get("name") for column in output.get("observed", []) if isinstance(column, dict)]
        return _markdown(f"Expected columns={expected}; observed columns={observed}; matches={output.get('matches')}.")
    if tool == "profile_table":
        return _markdown(
            f"Table={output.get('table')}; rows={output.get('row_count')}; "
            f"null counts={output.get('null_counts')}; duplicate order IDs={output.get('duplicate_order_ids')}."
        )
    if tool == "sample_rows":
        rows = output.get("rows", [])
        return _markdown(
            f"Inspected {len(rows) if isinstance(rows, list) else 0} bounded rows from "
            f"{output.get('table')}; raw rows are omitted from this report."
        )
    if tool == "verify_customer_key_normalization":
        verification = output.get("verification", {})
        return _markdown(
            f"Status={output.get('status')}; candidate={output.get('candidate_name')}; "
            f"unmatched keys {verification.get('unmatched_keys_before')}→"
            f"{verification.get('unmatched_keys_after')}; checks passed={verification.get('checks_passed')}."
        )
    return "A bounded structured tool result was returned."


def _yes_no(value: Any) -> str:
    return "Yes" if value is True else "No"


def render_markdown(
    investigation: dict[str, Any],
    remediation: dict[str, Any] | None = None,
    *,
    include_debug: bool = False,
) -> str:
    """Validate references and render a deterministic report without model calls."""
    report = _object(investigation.get("report"), "report")
    trace = _items(investigation.get("trace"), "trace")
    run_id = _text(report.get("run_id"), "report.run_id")
    category = _text(report.get("root_cause_category"), "report.root_cause_category")
    uncertainty = _text(report.get("uncertainty"), "report.uncertainty")
    if category not in ROOT_CAUSE_LABELS:
        raise ValueError(f"unsupported root-cause category: {category!r}")
    if uncertainty not in UNCERTAINTY_LABELS:
        raise ValueError(f"unsupported uncertainty: {uncertainty!r}")
    if report.get("changes_made") is not False:
        raise ValueError("incident report must explicitly state changes_made=false")

    results, trail = _tool_results(trace)
    evidence = [_object(item, "evidence reference") for item in _items(
        report.get("evidence_references"), "report.evidence_references"
    )]
    unknown = [item.get("reference_id") for item in evidence if item.get("reference_id") not in results]
    if unknown:
        raise ValueError(f"report cites unknown tool references: {unknown}")
    if remediation is None:
        embedded = [
            detail.get("output")
            for detail in results.values()
            if detail.get("tool") == "verify_customer_key_normalization"
            and isinstance(detail.get("output"), dict)
        ]
        if len(embedded) > 1:
            raise ValueError("trace contains more than one sandbox remediation result")
        if embedded:
            remediation = embedded[0]
    if remediation is not None:
        proposal_run_id = _text(remediation.get("run_id"), "remediation.run_id")
        if proposal_run_id != run_id:
            raise ValueError("remediation proposal belongs to a different run")
        if remediation.get("changes_made") is not False or remediation.get("sandbox_only") is not True:
            raise ValueError("remediation proposal is not a non-mutating sandbox result")

    tool_calls = investigation.get("tool_call_count", investigation.get("tool_calls", len(results)))
    model_steps = investigation.get("model_step_count", investigation.get("model_steps", "n/a"))
    model_calls = investigation.get("model_api_calls", "n/a")
    approval_required = bool(remediation and remediation.get("approval_required") is True)
    status_parts = ["Diagnosis complete", "No changes made"]
    if approval_required:
        status_parts.insert(1, "Human approval required")

    lines = [
        f"# Incident Report: {_markdown(run_id)}",
        "",
        f"> **Status:** {' · '.join(status_parts)}",
        "",
        "## Overview",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Run ID | `{_markdown(run_id)}` |",
        f"| Root-cause category | {ROOT_CAUSE_LABELS[category]} |",
        f"| Uncertainty | {UNCERTAINTY_LABELS[uncertainty]} |",
        f"| Changes made | {_yes_no(report.get('changes_made'))} |",
        "",
        "## What happened",
        "",
        _markdown(_text(report.get("explanation"), "report.explanation")),
        "",
        "## Evidence",
        "",
    ]
    for index, item in enumerate(evidence, start=1):
        reference_id = _text(item.get("reference_id"), "evidence reference_id")
        claim = _text(item.get("claim"), "evidence claim")
        result = results[reference_id]
        tool = _text(result.get("tool"), "evidence tool")
        lines.extend([
            f"{index}. **`{_markdown(reference_id)}` · `{_markdown(tool)}`**",
            f"   - Finding: {_markdown(claim)}",
            f"   - Observed: {_evidence_summary(tool, result.get('output'), result.get('error'))}",
        ])

    lines.extend([
        "",
        "## Recommended human action",
        "",
        _markdown(_text(report.get("proposed_human_action"), "report.proposed_human_action")),
        "",
        "## Sandboxed remediation",
        "",
    ])
    if remediation is None:
        lines.append("No sandbox remediation proposal was attached to this report.")
    else:
        verification = _object(remediation.get("verification"), "remediation.verification")
        lines.extend([
            "| Field | Value |",
            "| --- | --- |",
            f"| Status | {_markdown(remediation.get('status'))} |",
            f"| Candidate | `{_markdown(remediation.get('candidate_name'))}` |",
            f"| Checks passed | {_yes_no(verification.get('checks_passed'))} |",
            f"| Approval required | {_yes_no(remediation.get('approval_required'))} |",
            f"| Sandbox only | {_yes_no(remediation.get('sandbox_only'))} |",
            f"| Changes made | {_yes_no(remediation.get('changes_made'))} |",
            "",
            "**Candidate change:** " + _markdown(remediation.get("candidate_change")),
            "",
            _markdown(_text(remediation.get("explanation"), "remediation.explanation")),
            "",
            "### Verification checks",
            "",
            "| Check | Before | After |",
            "| --- | ---: | ---: |",
            f"| Row count | {verification.get('row_count_before')} | {verification.get('row_count_after')} |",
            f"| Unmatched customer keys | {verification.get('unmatched_keys_before')} | {verification.get('unmatched_keys_after')} |",
            f"| Null customer keys | {verification.get('null_keys_before')} | {verification.get('null_keys_after')} |",
            f"| Changed rows | — | {verification.get('changed_rows')} |",
            f"| Normalization collisions | — | {verification.get('source_normalization_collisions')} |",
            f"| Reference duplicate keys | — | {verification.get('reference_duplicate_keys')} |",
            f"| Reference noncanonical keys | — | {verification.get('reference_noncanonical_keys')} |",
            "",
        ])
        preview = _items(remediation.get("change_preview", []), "remediation.change_preview")
        if preview:
            lines.extend([
                "### Change preview",
                "",
                "| Order ID | Before | After |",
                "| --- | --- | --- |",
            ])
            for raw_change in preview:
                change = _object(raw_change, "change preview item")
                lines.append(
                    f"| `{_markdown(change.get('order_id'))}` | `{_markdown(change.get('before'))}` | "
                    f"`{_markdown(change.get('after'))}` |"
                )
            lines.append("")
        lines.extend(["### Assumptions requiring review", ""])
        for assumption in _items(remediation.get("assumptions"), "remediation.assumptions"):
            lines.append(f"- {_markdown(assumption)}")
        blockers = _items(verification.get("blockers", []), "remediation.verification.blockers")
        if blockers:
            lines.extend(["", "### Blocking checks", ""])
            for blocker in blockers:
                lines.append(f"- {_markdown(blocker)}")
        lines.extend([
            "",
            "**Remediation handoff:** "
            + _markdown(_text(remediation.get("proposed_human_action"), "remediation.proposed_human_action")),
        ])

    if include_debug:
        lines.extend([
            "",
            "## Technical appendix",
            "",
            "This section is for agent development and evaluation.",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| Evidence tool calls | {_markdown(tool_calls)} |",
            f"| Model steps | {_markdown(model_steps)} |",
            f"| Model API calls | {_markdown(model_calls)} |",
            "",
            "### Tool execution trail",
            "",
            "| Step | Tool | Result |",
            "| ---: | --- | --- |",
        ])
        for step, tool, result_status in trail:
            lines.append(f"| {step} | `{_markdown(tool)}` | {result_status} |")
    lines.extend([
        "",
        "---",
        "",
        "Generated deterministically from saved investigation artifacts. Rendering made no model calls and changed no pipeline data.",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a saved investigation as a Markdown incident report")
    parser.add_argument("investigation_json", type=Path)
    parser.add_argument("--run-id", help="Required when investigation_json is an evaluation result")
    parser.add_argument("--remediation-json", type=Path)
    parser.add_argument(
        "--include-debug",
        action="store_true",
        help="Append model counters and the tool execution trail for agent development",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    investigation = load_investigation(args.investigation_json, args.run_id)
    run_id = investigation["report"]["run_id"]
    remediation = load_remediation(args.remediation_json, run_id) if args.remediation_json else None
    rendered = render_markdown(investigation, remediation, include_debug=args.include_debug)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "run_id": run_id,
        "evidence_count": len(investigation["report"]["evidence_references"]),
        "remediation_included": remediation is not None,
        "debug_included": args.include_debug,
        "model_api_calls": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
