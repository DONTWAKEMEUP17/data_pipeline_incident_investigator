"""Small read-only local web view for generated Airflow incident artifacts."""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


EVENT_ID_PATTERN = re.compile(r"^[a-f0-9]{24}$")
MAX_JSON_BYTES = 256_000
MAX_REPORT_BYTES = 128_000
CATEGORY_LABELS = {
    "schema_drift": "Schema drift",
    "data_quality": "Data quality",
    "join_reference": "Join / reference",
    "freshness_volume": "Freshness / volume",
    "unknown": "Unknown / abstained",
}
UNCERTAINTY_LABELS = {"low": "Low", "medium": "Medium", "high": "High"}


@dataclass(frozen=True)
class IncidentArtifact:
    event_id: str
    manifest: dict[str, Any]
    investigation: dict[str, Any]
    report_markdown: str

    @property
    def report(self) -> dict[str, Any]:
        return self.investigation["report"]


@dataclass(frozen=True)
class IncidentScan:
    incidents: tuple[IncidentArtifact, ...]
    unreadable_count: int


@dataclass(frozen=True)
class WebResponse:
    status: int
    content_type: str
    body: bytes


class IncidentRepository:
    """Validate and read canonical incident output without modifying it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def _read_file(path: Path, maximum_bytes: int) -> str:
        if path.is_symlink() or not path.is_file():
            raise ValueError("incident artifact must be a regular file")
        if path.stat().st_size > maximum_bytes:
            raise ValueError("incident artifact exceeds the display limit")
        return path.read_text(encoding="utf-8")

    def _load(self, event_id: str) -> IncidentArtifact:
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            raise ValueError("invalid event ID")
        incident_dir = self.root / event_id
        if incident_dir.is_symlink() or not incident_dir.is_dir():
            raise ValueError("incident directory is missing or unsafe")
        manifest = json.loads(self._read_file(incident_dir / "manifest.json", MAX_JSON_BYTES))
        if not isinstance(manifest, dict):
            raise ValueError("incident manifest must be an object")
        if manifest.get("state") != "complete" or manifest.get("event_id") != event_id:
            raise ValueError("incident manifest is incomplete or inconsistent")
        if manifest.get("report_file") != "report.md" or manifest.get("investigation_file") != "investigation.json":
            raise ValueError("incident manifest points outside the canonical files")
        investigation = json.loads(self._read_file(incident_dir / "investigation.json", MAX_JSON_BYTES))
        if not isinstance(investigation, dict) or not isinstance(investigation.get("report"), dict):
            raise ValueError("investigation artifact has an invalid shape")
        report = investigation["report"]
        if report.get("run_id") != manifest.get("airflow_run_id"):
            raise ValueError("investigation and Airflow run IDs do not match")
        if report.get("root_cause_category") not in CATEGORY_LABELS:
            raise ValueError("investigation has an unsupported root-cause category")
        if report.get("uncertainty") not in UNCERTAINTY_LABELS:
            raise ValueError("investigation has an unsupported uncertainty")
        if report.get("changes_made") is not False:
            raise ValueError("incident report must state that no changes were made")
        if not isinstance(report.get("evidence_references"), list) or not isinstance(investigation.get("trace"), list):
            raise ValueError("investigation evidence has an invalid shape")
        tool_results = _tool_results(investigation)
        if any(
            not isinstance(reference, dict)
            or not isinstance(reference.get("reference_id"), str)
            or reference["reference_id"] not in tool_results
            or not isinstance(reference.get("claim"), str)
            for reference in report["evidence_references"]
        ):
            raise ValueError("investigation cites missing or invalid evidence")
        markdown = self._read_file(incident_dir / "report.md", MAX_REPORT_BYTES)
        return IncidentArtifact(event_id, manifest, investigation, markdown)

    def scan(self) -> IncidentScan:
        if not self.root.is_dir():
            return IncidentScan((), 0)
        incidents: list[IncidentArtifact] = []
        unreadable = 0
        for path in self.root.iterdir():
            if not path.is_dir() or not EVENT_ID_PATTERN.fullmatch(path.name):
                continue
            try:
                incidents.append(self._load(path.name))
            except (OSError, ValueError, json.JSONDecodeError):
                unreadable += 1
        incidents.sort(key=lambda item: str(item.manifest["airflow_run_id"]), reverse=True)
        return IncidentScan(tuple(incidents), unreadable)

    def get(self, event_id: str) -> IncidentArtifact | None:
        try:
            return self._load(event_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return None


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _display_time(run_id: Any) -> str:
    if not isinstance(run_id, str):
        return "Unknown time"
    raw = run_id.removeprefix("manual__")
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return run_id


def _styles() -> str:
    return """
    :root { color-scheme: light; --ink:#15232d; --muted:#61717d; --line:#dce4e7;
      --paper:#fbfcfa; --panel:#ffffff; --accent:#0c7067; --accent-soft:#e3f2ef;
      --danger:#a13f2d; --danger-soft:#f8e9e4; --shadow:0 18px 50px rgba(20,42,52,.08); }
    * { box-sizing:border-box; }
    body { margin:0; background:linear-gradient(140deg,#eef5f1 0,#f7f4ed 48%,#edf2f4 100%);
      color:var(--ink); font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    a { color:var(--accent); }
    .shell { width:min(1120px,calc(100% - 32px)); margin:0 auto; padding:32px 0 64px; }
    .masthead { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:28px; }
    .brand { display:flex; align-items:center; gap:13px; color:inherit; text-decoration:none; }
    .brand .mark { width:42px; height:42px; display:grid; place-items:center; border-radius:13px;
      background:var(--ink); color:white; font-weight:800; letter-spacing:-.04em; }
    .brand strong { display:block; font-size:15px; letter-spacing:.01em; }
    .brand span { display:block; color:var(--muted); font-size:12px; }
    .readonly { padding:7px 11px; border:1px solid #b9d8d2; border-radius:999px;
      background:rgba(255,255,255,.7); color:var(--accent); font-size:12px; font-weight:700; }
    .hero { padding:34px; border:1px solid rgba(255,255,255,.9); border-radius:24px;
      background:rgba(255,255,255,.78); box-shadow:var(--shadow); backdrop-filter:blur(10px); }
    h1 { margin:0; max-width:790px; font-size:clamp(32px,5vw,58px); line-height:1.02; letter-spacing:-.045em; }
    h2 { margin:0 0 14px; font-size:22px; letter-spacing:-.025em; }
    h3 { margin:0; font-size:16px; }
    .hero p { max-width:700px; margin:18px 0 0; color:var(--muted); font-size:17px; }
    .section-head { display:flex; justify-content:space-between; align-items:end; gap:16px; margin:34px 2px 14px; }
    .section-head p { margin:0; color:var(--muted); }
    .incident-list { display:grid; gap:12px; }
    .incident-row { display:grid; grid-template-columns:minmax(240px,1.4fr) minmax(150px,.7fr) 120px 28px;
      align-items:center; gap:20px; padding:19px 22px; color:inherit; text-decoration:none;
      border:1px solid var(--line); border-radius:16px; background:var(--panel); box-shadow:0 6px 20px rgba(20,42,52,.04); }
    .incident-row:hover { transform:translateY(-1px); border-color:#a9cbc5; box-shadow:0 12px 30px rgba(20,42,52,.08); }
    .eyebrow { color:var(--muted); font-size:12px; font-weight:750; letter-spacing:.08em; text-transform:uppercase; }
    .run-id { margin-top:3px; font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-wrap:anywhere; }
    .meta { color:var(--muted); font-size:13px; }
    .badge { display:inline-flex; width:max-content; align-items:center; gap:7px; padding:6px 10px;
      border-radius:999px; background:var(--danger-soft); color:var(--danger); font-size:12px; font-weight:800; }
    .badge::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; }
    .arrow { font-size:22px; color:var(--accent); }
    .empty { padding:48px; text-align:center; border:1px dashed #b9c6ca; border-radius:18px;
      background:rgba(255,255,255,.55); color:var(--muted); }
    .notice { margin:14px 0; padding:12px 14px; border-radius:12px; background:#fff4d9; color:#73581d; }
    .back { display:inline-flex; margin:4px 0 22px; text-decoration:none; font-weight:700; }
    .detail-head { padding:30px; border-radius:22px; background:var(--ink); color:white; box-shadow:var(--shadow); }
    .detail-head .eyebrow { color:#a8c5c7; }
    .detail-head h1 { margin-top:8px; font-size:clamp(28px,4vw,48px); }
    .detail-head p { color:#c9d8dc; overflow-wrap:anywhere; }
    .status-line { display:flex; flex-wrap:wrap; gap:9px; margin-top:22px; }
    .status-chip { padding:7px 10px; border-radius:999px; background:rgba(255,255,255,.11); font-size:12px; font-weight:750; }
    .status-chip.good { background:#dff3e8; color:#176343; }
    .detail-grid { display:grid; grid-template-columns:minmax(0,1.55fr) minmax(260px,.65fr); gap:18px; margin-top:18px; }
    .stack { display:grid; gap:18px; align-content:start; }
    .card { padding:24px; border:1px solid var(--line); border-radius:18px; background:var(--panel); box-shadow:0 8px 26px rgba(20,42,52,.045); }
    .card p { margin:0; }
    .action { border-left:5px solid var(--accent); }
    .evidence { display:grid; gap:12px; margin-top:14px; }
    .evidence-item { padding:16px; border:1px solid var(--line); border-radius:13px; background:var(--paper); }
    .evidence-item p { margin-top:8px; color:var(--muted); }
    .tool { font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--accent); }
    dl { display:grid; grid-template-columns:115px minmax(0,1fr); gap:10px 14px; margin:0; }
    dt { color:var(--muted); }
    dd { margin:0; overflow-wrap:anywhere; }
    .report-link { display:inline-flex; margin-top:18px; font-weight:750; }
    footer { margin-top:30px; color:var(--muted); font-size:12px; text-align:center; }
    @media (max-width:760px) { .incident-row { grid-template-columns:1fr auto; }
      .incident-row .meta:nth-of-type(2) { display:none; } .detail-grid { grid-template-columns:1fr; }
      .hero,.detail-head { padding:24px; } .shell { width:min(100% - 22px,1120px); } }
    """


def _page(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(title)}</title><style>{_styles()}</style></head>
<body><main class="shell"><header class="masthead"><a class="brand" href="/"><span class="mark">PI</span>
<span><strong>Pipeline Incident Investigator</strong><span>Local Airflow prototype</span></span></a>
<span class="readonly">Read-only view</span></header>{content}
<footer>Synthetic local incidents · Recommendations require human review · No pipeline changes from this view</footer>
</main></body></html>"""


def _tool_results(investigation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for event in investigation.get("trace", []):
        if isinstance(event, dict) and event.get("kind") == "tool_result":
            detail = event.get("detail")
            if isinstance(detail, dict) and isinstance(detail.get("reference_id"), str):
                results[detail["reference_id"]] = detail
    return results


def _evidence_observation(tool: Any, output: Any, error: Any) -> str:
    if error:
        return f"Tool error: {error}"
    if not isinstance(output, dict):
        return "No structured output was returned."
    if tool == "compare_schema":
        expected = [item.get("name") for item in output.get("expected", []) if isinstance(item, dict)]
        observed = [item.get("name") for item in output.get("observed", []) if isinstance(item, dict)]
        return f"Expected columns: {expected}. Observed columns: {observed}. Match: {output.get('matches')}."
    if tool == "profile_table":
        return (
            f"Rows: {output.get('row_count')}. Duplicate order IDs: {output.get('duplicate_order_ids')}. "
            f"Null counts: {output.get('null_counts')}."
        )
    if tool == "get_run_summary":
        stages = output.get("stages", {})
        states = ", ".join(
            f"{name}={value.get('status')}" for name, value in stages.items() if isinstance(value, dict)
        )
        return f"Pipeline status: {output.get('status')}. Stages: {states}."
    if tool == "read_stage_log":
        lines = output.get("lines", [])
        return " ".join(
            f"L{line.get('number')}: {line.get('text')}" for line in lines if isinstance(line, dict)
        ) or "The bounded log excerpt was empty."
    if tool == "sample_rows":
        rows = output.get("rows", [])
        return f"Inspected {len(rows) if isinstance(rows, list) else 0} bounded rows; row values are hidden here."
    return "A bounded structured result was returned."


def render_list(scan: IncidentScan) -> str:
    rows = []
    for incident in scan.incidents:
        manifest = incident.manifest
        report = incident.report
        category = CATEGORY_LABELS[report["root_cause_category"]]
        rows.append(f"""<a class="incident-row" href="/incidents/{incident.event_id}">
<div><div class="eyebrow">{_escape(_display_time(manifest.get('airflow_run_id')))}</div>
<div class="run-id">{_escape(manifest.get('airflow_run_id'))}</div></div>
<div><span class="badge">{_escape(category)}</span><div class="meta">Uncertainty: {_escape(UNCERTAINTY_LABELS[report['uncertainty']])}</div></div>
<div class="meta">{_escape(manifest.get('task_id'))}<br>Attempt {_escape(manifest.get('try_number'))}</div>
<div class="arrow">›</div></a>""")
    if rows:
        listing = f'<div class="incident-list">{"".join(rows)}</div>'
    else:
        listing = '<div class="empty"><h3>No completed incidents yet</h3><p>Run the Milestone 4B smoke command, then refresh this page.</p></div>'
    warning = (
        f'<div class="notice">{scan.unreadable_count} incident artifact(s) could not be read and were omitted.</div>'
        if scan.unreadable_count else ""
    )
    return _page("Incidents", f"""<section class="hero"><div class="eyebrow">Incident workspace</div>
<h1>Start with the evidence that changes the next action.</h1>
<p>Failed Airflow tasks appear here after the separate investigator finishes. Open a report to review the diagnosis, cited evidence, and proposed human action.</p></section>
<div class="section-head"><div><div class="eyebrow">Completed investigations</div><h2>{len(scan.incidents)} incidents</h2></div>
<p>Newest Airflow run first</p></div>{warning}{listing}""")


def render_detail(incident: IncidentArtifact) -> str:
    manifest = incident.manifest
    report = incident.report
    results = _tool_results(incident.investigation)
    evidence_html = []
    for reference in report["evidence_references"]:
        if not isinstance(reference, dict):
            continue
        reference_id = reference.get("reference_id")
        detail = results.get(reference_id, {})
        tool = detail.get("tool", "unknown")
        observed = _evidence_observation(tool, detail.get("output"), detail.get("error"))
        evidence_html.append(f"""<article class="evidence-item" id="evidence-{_escape(reference_id)}">
<div class="tool">{_escape(reference_id)} · {_escape(tool)}</div><h3>{_escape(reference.get('claim'))}</h3>
<p>{_escape(observed)}</p></article>""")
    evidence = "".join(evidence_html) or '<p class="meta">No cited evidence was available.</p>'
    category = CATEGORY_LABELS[report["root_cause_category"]]
    uncertainty = UNCERTAINTY_LABELS[report["uncertainty"]]
    content = f"""<a class="back" href="/">← All incidents</a>
<section class="detail-head"><div class="eyebrow">{_escape(_display_time(manifest.get('airflow_run_id')))}</div>
<h1>{_escape(category)}</h1><p>{_escape(manifest.get('airflow_run_id'))}</p>
<div class="status-line"><span class="status-chip good">Diagnosis complete</span>
<span class="status-chip">{_escape(uncertainty)} uncertainty</span><span class="status-chip">No changes made</span></div></section>
<div class="detail-grid"><div class="stack">
<section class="card"><div class="eyebrow">What happened</div><h2>Diagnosis</h2><p>{_escape(report.get('explanation'))}</p></section>
<section class="card action"><div class="eyebrow">Recommended human action</div><h2>Next step</h2><p>{_escape(report.get('proposed_human_action'))}</p></section>
<section class="card"><div class="eyebrow">Grounded evidence</div><h2>{len(evidence_html)} cited observations</h2>
<div class="evidence">{evidence}</div></section></div>
<aside class="stack"><section class="card"><div class="eyebrow">Airflow context</div><h2>Failed attempt</h2><dl>
<dt>DAG</dt><dd>{_escape(manifest.get('dag_id'))}</dd><dt>Task</dt><dd>{_escape(manifest.get('task_id'))}</dd>
<dt>Attempt</dt><dd>{_escape(manifest.get('try_number'))}</dd><dt>Event</dt><dd>{_escape(incident.event_id)}</dd></dl></section>
<section class="card"><div class="eyebrow">Investigator</div><h2>Run details</h2><dl>
<dt>Adapter</dt><dd>{_escape(manifest.get('adapter'))}</dd><dt>API calls</dt><dd>{_escape(manifest.get('model_api_calls'))}</dd>
<dt>Data changed</dt><dd>No</dd></dl><a class="report-link" href="/incidents/{incident.event_id}/report.md">Open Markdown report →</a></section></aside></div>"""
    return _page(f"Incident {incident.event_id}", content)


class IncidentWebApp:
    def __init__(self, repository: IncidentRepository) -> None:
        self.repository = repository

    def respond(self, method: str, raw_path: str) -> WebResponse:
        if method not in {"GET", "HEAD"}:
            return WebResponse(405, "text/plain; charset=utf-8", b"Method not allowed\n")
        path = unquote(urlsplit(raw_path).path)
        if path == "/healthz":
            scan = self.repository.scan()
            body = json.dumps({
                "status": "ok",
                "read_only": True,
                "incident_count": len(scan.incidents),
                "unreadable_count": scan.unreadable_count,
            }, sort_keys=True).encode("utf-8") + b"\n"
            return WebResponse(200, "application/json; charset=utf-8", body)
        if path == "/":
            return WebResponse(200, "text/html; charset=utf-8", render_list(self.repository.scan()).encode("utf-8"))
        match = re.fullmatch(r"/incidents/([a-f0-9]{24})(/report\.md)?", path)
        if match:
            incident = self.repository.get(match.group(1))
            if incident is not None:
                if match.group(2):
                    return WebResponse(200, "text/markdown; charset=utf-8", incident.report_markdown.encode("utf-8"))
                return WebResponse(200, "text/html; charset=utf-8", render_detail(incident).encode("utf-8"))
        return WebResponse(404, "text/plain; charset=utf-8", b"Incident not found\n")


def create_server(host: str, port: int, incidents_dir: Path) -> ThreadingHTTPServer:
    app = IncidentWebApp(IncidentRepository(incidents_dir))

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            response = app.respond(self.command, self.path)
            body = b"" if self.command == "HEAD" else response.body
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if body:
                self.wfile.write(body)

        do_GET = _serve
        do_HEAD = _serve
        do_POST = _serve
        do_PUT = _serve
        do_PATCH = _serve
        do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:
            print(f"web {self.address_string()} {format % args}")

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the read-only local incident list and report pages")
    parser.add_argument("--incidents-dir", type=Path, default=Path("airflow_incidents/incidents"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.incidents_dir)
    print(f"Read-only incident view: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
