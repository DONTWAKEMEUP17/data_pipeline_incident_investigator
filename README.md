# Data Pipeline Incident Investigator — Milestones 0–2

This is a local learning prototype. It generates four synthetic CSV-to-DuckDB batch runs, offers bounded read-only evidence tools and a simple failure baseline, and runs a minimal bounded investigation loop. Its current decision adapter is deterministic local code used to learn the agent mechanics; it is not an LLM. There is no external model call, Airflow integration, report UI, or external data connection yet.

## Run locally

Requires Python 3.9 or newer. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m incident_pipeline --output-dir artifacts
.venv/bin/python -m incident_pipeline.baseline failed_001
.venv/bin/python -m incident_pipeline.baseline failed_002
.venv/bin/python -m incident_pipeline.agent failed_001
.venv/bin/python -m incident_pipeline.agent ambiguous_001
.venv/bin/python -m unittest discover -s tests -v
```

The generator accepts a different output directory via `--output-dir`. It regenerates the four known runs and overwrites only their known generated files. It needs no API key. The CSV fixtures are made-up orders, not personal data.

## Inspect the runs

- `fixtures/` contains the source CSVs. Each run also stores its own `input.csv` snapshot.
- `pipeline_contract.json` defines expected columns and the three validation checks.
- `artifacts/<run_id>/summary.json` records overall and per-stage status.
- `artifacts/<run_id>/logs/` gives a short message for each stage.
- `artifacts/<run_id>/schemas.json` records the tables that were actually created.
- `artifacts/<run_id>/validation.json` records check outcomes, or `skipped` when transform failed.
- `artifacts/<run_id>/pipeline.duckdb` holds the local raw table and, when transform succeeds, the transformed table.

The current runs are `healthy_001` (no failure), `failed_001` (schema drift plus a duplicate ID), `failed_002` (the same six rows with the expected schema and a duplicate ID), and `ambiguous_001` (an empty batch with insufficient evidence to name its cause). A CSV has only one header: the first line. A later header-like line is data, so the generator rejects repeated headers before writing artifacts. `0-004` uses digit zero and is distinct from IDs beginning with letter `O`.

Try reading the failed run's `input.csv`, `schemas.json`, transform log, and the contract. Predict the cause before reading the code. The run artifacts deliberately contain observations rather than an answer key.

## How the pipeline works

`ingest` loads the snapshot as strings into `raw_orders`. `transform` compares the observed raw schema with the contract before creating typed `orders_daily`. `validate` checks that the result has rows, required fields are present, and order IDs are unique. A schema mismatch stops the run at transform and leaves validation skipped. Text artifacts are byte-for-byte reproducible; DuckDB contents are checked logically because its database file bytes are not part of the fixture contract.

## Architecture note

The saved run directory is the evidence boundary. `LocalArtifactProvider` implements the typed `RunEvidenceProvider` interface using five methods: `get_run_summary`, `read_stage_log`, `compare_schema`, `profile_table`, and `sample_rows`. Keeping observations on disk makes a failure reproducible without running the pipeline again. A later Airflow adapter could implement the same interface, while the investigator stays separate. Airflow, evaluation, a real model provider, and the report UI remain outside this slice.

## Read-only evidence tools and baseline

For a quick manual inspection from the repository root:

```sh
.venv/bin/python -c 'from incident_pipeline.evidence import LocalArtifactProvider; p = LocalArtifactProvider(); print(p.compare_schema("failed_001", "raw_orders")); print(p.read_stage_log("failed_001", "transform", 5))'
```

The provider accepts only registered run IDs, contract table names, and pipeline stage names. It opens DuckDB in read-only mode and has no SQL-taking method. Logs return at most 20 numbered lines with at most 240 characters per line. Samples return at most five rows with at most 120 characters per cell. Profiles and samples reject tables with more than 16 columns or column names longer than 80 characters. Larger requested limits and unknown names raise `ValueError`. Logs and sample values are evidence data, not instructions to follow.

The baseline prints JSON, not a diagnosis report. It returns the first failed validation check with a short validation log excerpt. If validation was skipped, it returns the first failed stage with that stage's log excerpt. A healthy run returns `No failure found`. This fixed rule is meant to be a transparent comparison point for later agent evaluation; it does not infer a root cause.

## Minimal investigation loop

`incident_pipeline/agent.py` separates three responsibilities:

1. `ModelAdapter` chooses the next typed tool request or proposes a final report.
2. `investigate` enforces the tool and model-step budgets, executes only allowlisted tools, records each request/result, and checks evidence references.
3. `IncidentReport` carries the root-cause category, explanation, real evidence references, a proposed human action, uncertainty, and `changes_made: false`.

The included `DeterministicLearningAdapter` chooses based on returned observations. It takes different paths for schema drift, duplicate IDs, healthy runs, and empty batches. This makes the observe → decide → act loop inspectable and testable without credentials or cost. It does not prove that an LLM will behave correctly. Unit tests use mocked adapter responses to test invalid citations, output validation, and budget exhaustion.

Each investigation allows at most five tool calls, six decision steps, and 4,000 serialized characters per observation. Tool-level row, line, column, and cell bounds still apply. An invalid evidence citation, tool error, or exhausted budget produces a partial `unknown` report rather than an invented cause. The command prints the report and trace to stdout and does not save or modify pipeline data.

### Hands-on checkpoint

Run `.venv/bin/python -m incident_pipeline.agent failed_001` and walk through the trace. Explain why `compare_schema` follows `get_run_summary`, and why `profile_table` follows the schema mismatch. Then choose whether a future real-model policy should stop after finding the first blocker or continue within its budget to report additional anomalies such as the duplicate ID. The current local policy continues and labels the duplicate as an additional issue.
