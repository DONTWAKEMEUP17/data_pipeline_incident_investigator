# Milestone 4: local Airflow integration

Milestone 4A runs the existing synthetic `ingest → transform → validate` pipeline as an Airflow 3 TaskFlow DAG. Milestone 4B adds an identifier-only failure event, a separate investigator process, a read-only Airflow evidence adapter, and one human-readable report per failed task attempt.

## Why Docker

The project virtual environment uses Python 3.9. Airflow 3.3.2 supports Python 3.10–3.14, so the Airflow runtime is pinned to the official `apache/airflow:3.3.2-python3.12` image instead of changing the project environment. The custom image adds only the pinned DuckDB dependency needed by the pipeline.

This is a local development setup with SQLite metadata. It is not a production Airflow deployment. Airflow's official documentation describes both the supported Python versions and Docker/local-development boundaries:

- [Airflow prerequisites](https://airflow.apache.org/docs/apache-airflow/stable/installation/prerequisites.html)
- [Airflow installation choices](https://airflow.apache.org/docs/apache-airflow/stable/installation.html)
- [TaskFlow API tutorial](https://airflow.apache.org/docs/apache-airflow/stable/tutorial/taskflow.html)
- [Testing DAGs and `dag.test`](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html)
- [Airflow callbacks](https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/logging-monitoring/callbacks.html)

## DAG behavior

`dags/orders_daily_pipeline.py` exposes one manually triggered DAG with three tasks:

```text
ingest → transform → validate
```

The DAG accepts two allowlisted synthetic scenarios through run configuration:

- `healthy` uses `healthy_001` and completes all three tasks.
- `schema_drift` uses `failed_001`; transform saves the mismatch evidence and then raises `PipelineStageError`. Airflow marks transform failed and validate upstream-failed.

Each Airflow run receives a separate artifact directory. `airflow_run.json` records the DAG ID, Airflow run ID, scenario, pipeline run ID, and task attempts. Runtime databases, logs, and generated pipeline artifacts are ignored by Git.

## Failure handoff

The task-level `on_failure_callback` calls `emit_failure_event`. The callback saves exactly these fields:

```text
schema_version, event_id, dag_id, airflow_run_id, task_id, try_number
```

`event_id` is a hash of the four Airflow identifiers and acts as the idempotency key. Repeating the same callback writes the same event path. The callback catches handoff errors so they cannot replace the task's original exception.

The callback does not inspect evidence or call a model. The separate `incident_pipeline.airflow_incidents` process:

1. Validates the event and idempotency key.
2. Uses `AirflowEvidenceProvider` to locate the matching artifact directory and failed task attempt.
3. Delegates bounded schema, profile, log, and row reads to the existing read-only evidence tools.
4. Runs the existing agent loop with the deterministic local adapter.
5. Writes `investigation.json`, `report.md`, and `manifest.json` to one canonical incident directory.

If processing fails, the worker records a small failure file and continues with other events. It does not edit Airflow state, the source CSV, or the saved DuckDB database.

## Reproduce

Docker Desktop must be running. From the repository root:

```sh
bash scripts/airflow_m4a_smoke.sh
```

The first run downloads the official Airflow image. The script then:

1. Builds the pinned image.
2. Migrates a local SQLite metadata database.
3. Serializes the DAG and checks import errors.
4. Runs the healthy scenario.
5. Runs the schema-drift scenario and requires it to fail.
6. Verifies Airflow metadata and saved pipeline artifacts.

The normalized result is written to `airflow/results/m4a_verification.json`. The test uses Airflow's `dags test` integration path: real task callables execute and Airflow records DagRun/task states, but no long-running scheduler or browser UI is started in this milestone.

## Reproduce the callback-to-report handoff

```sh
bash scripts/airflow_m4b_smoke.sh
```

This script causes a real transform failure, checks that Airflow invokes the callback, then launches the investigator worker as a separate process. It runs the worker twice to verify idempotency. The checked-in normalized result is `airflow/results/m4b_verification.json`; runtime events, artifacts, and reports are ignored.

The reproducible smoke path uses the offline deterministic adapter, so it needs no API key and spends no model credits. It validates the integration boundary rather than re-measuring the frozen model benchmark.

The separate worker also accepts the existing bounded OpenAI adapter when explicitly selected:

```sh
.venv/bin/python -m incident_pipeline.airflow_incidents \
  --adapter openai \
  --model gpt-5.6-terra \
  --max-api-calls 4 \
  --max-output-tokens 700 \
  --reasoning-effort low
```

Already completed event IDs are skipped, regardless of adapter choice. The callback itself never receives API credentials and never calls the model.
