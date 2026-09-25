# Milestone 4A: local Airflow orchestration

This slice runs the existing synthetic `ingest → transform → validate` pipeline as an Airflow 3 TaskFlow DAG. It deliberately stops before failure callbacks, an Airflow evidence adapter, agent calls, and automated report generation.

## Why Docker

The project virtual environment uses Python 3.9. Airflow 3.3.2 supports Python 3.10–3.14, so the Airflow runtime is pinned to the official `apache/airflow:3.3.2-python3.12` image instead of changing the project environment. The custom image adds only the pinned DuckDB dependency needed by the pipeline.

This is a local development setup with SQLite metadata. It is not a production Airflow deployment. Airflow's official documentation describes both the supported Python versions and Docker/local-development boundaries:

- [Airflow prerequisites](https://airflow.apache.org/docs/apache-airflow/stable/installation/prerequisites.html)
- [Airflow installation choices](https://airflow.apache.org/docs/apache-airflow/stable/installation.html)
- [TaskFlow API tutorial](https://airflow.apache.org/docs/apache-airflow/stable/tutorial/taskflow.html)
- [Testing DAGs and `dag.test`](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html)

## DAG behavior

`dags/orders_daily_pipeline.py` exposes one manually triggered DAG with three tasks:

```text
ingest → transform → validate
```

The DAG accepts two allowlisted synthetic scenarios through run configuration:

- `healthy` uses `healthy_001` and completes all three tasks.
- `schema_drift` uses `failed_001`; transform saves the mismatch evidence and then raises `PipelineStageError`. Airflow marks transform failed and validate upstream-failed.

Each Airflow run receives a separate artifact directory. `airflow_run.json` records the DAG ID, Airflow run ID, scenario, pipeline run ID, and task attempts. Runtime databases, logs, and generated pipeline artifacts are ignored by Git.

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

