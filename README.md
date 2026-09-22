# Data Pipeline Incident Investigator — Milestones 0–1

This is a local learning prototype. It generates two synthetic CSV-to-DuckDB batch runs, then offers bounded, read-only evidence tools and a simple failure baseline. There is no agent, incident report, model call, Airflow integration, or external data connection yet.

## Run locally

Requires Python 3.9 or newer. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m incident_pipeline --output-dir artifacts
.venv/bin/python -m incident_pipeline.baseline failed_001
.venv/bin/python -m unittest discover -s tests -v
```

The generator accepts a different output directory via `--output-dir`. It regenerates the two known runs and overwrites only their known generated files. It needs no API key. The CSV fixtures are made-up orders, not personal data.

## Inspect the runs

- `fixtures/` contains the two source CSVs. Each run also stores its own `input.csv` snapshot.
- `pipeline_contract.json` defines expected columns and the three validation checks.
- `artifacts/<run_id>/summary.json` records overall and per-stage status.
- `artifacts/<run_id>/logs/` gives a short message for each stage.
- `artifacts/<run_id>/schemas.json` records the tables that were actually created.
- `artifacts/<run_id>/validation.json` records check outcomes, or `skipped` when transform failed.
- `artifacts/<run_id>/pipeline.duckdb` holds the local raw table and, for the healthy run, the transformed table.

Try reading the failed run's `input.csv`, `schemas.json`, transform log, and the contract. Predict the cause before reading the code. The run artifacts deliberately contain observations rather than an answer key.

## How the pipeline works

`ingest` loads the snapshot as strings into `raw_orders`. `transform` compares the observed raw schema with the contract before creating typed `orders_daily`. `validate` checks that the result has rows, required fields are present, and order IDs are unique. A schema mismatch stops the run at transform and leaves validation skipped. Text artifacts are byte-for-byte reproducible; DuckDB contents are checked logically because its database file bytes are not part of the fixture contract.

## Architecture note

The saved run directory is the evidence boundary. `LocalArtifactProvider` implements the typed `RunEvidenceProvider` interface using five methods: `get_run_summary`, `read_stage_log`, `compare_schema`, `profile_table`, and `sample_rows`. Keeping observations on disk makes a failure reproducible without running the pipeline again. A later Airflow adapter could implement the same interface, while the investigator stays separate. That adapter, the agent, evaluation, and the human-readable report are outside this slice.

## Read-only evidence tools and baseline

For a quick manual inspection from the repository root:

```sh
.venv/bin/python -c 'from incident_pipeline.evidence import LocalArtifactProvider; p = LocalArtifactProvider(); print(p.compare_schema("failed_001", "raw_orders")); print(p.read_stage_log("failed_001", "transform", 5))'
```

The provider accepts only registered run IDs, contract table names, and pipeline stage names. It opens DuckDB in read-only mode and has no SQL-taking method. Logs return at most 20 numbered lines with at most 240 characters per line. Samples return at most five rows with at most 120 characters per cell. Profiles and samples reject tables with more than 16 columns or column names longer than 80 characters. Larger requested limits and unknown names raise `ValueError`. Logs and sample values are evidence data, not instructions to follow.

The baseline prints JSON, not a diagnosis report. It returns the first failed validation check with a short validation log excerpt. If validation was skipped, it returns the first failed stage with that stage's log excerpt. A healthy run returns `No failure found`. This fixed rule is meant to be a transparent comparison point for later agent evaluation; it does not infer a root cause.

### Hands-on checkpoint

Add or modify one **synthetic** fixture in `fixtures/`, register it in `RUN_INPUTS` in `incident_pipeline/pipeline.py`, and regenerate the artifacts. Before calling an evidence tool, predict its result. For example, if you add a duplicate `order_id` to a fixture with the expected header, what should `profile_table` and `get_run_summary` show? Keep any new run ID distinct and do not use real data.
