"""Airflow 3 TaskFlow DAG for the existing synthetic orders pipeline."""

from __future__ import annotations

import os
from pathlib import Path

import pendulum
from airflow.sdk import dag, get_current_context, task


ARTIFACT_ROOT_ENV = "INCIDENT_PIPELINE_AIRFLOW_ARTIFACTS_DIR"


def _try_number(context: dict) -> int:
    task_instance = context.get("task_instance")
    return int(getattr(task_instance, "try_number", 1))


@dag(
    dag_id="orders_daily_pipeline",
    schedule=None,
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    params={"scenario": "healthy"},
    tags=["local-prototype", "synthetic"],
)
def orders_daily_pipeline():
    @task(task_id="ingest", retries=0)
    def ingest() -> dict[str, str]:
        from incident_pipeline.airflow_runtime import descriptor_for_context, record_task_attempt
        from incident_pipeline.pipeline import ingest_stage

        context = get_current_context()
        root = Path(os.environ.get(ARTIFACT_ROOT_ENV, "/opt/airflow/project/airflow_artifacts"))
        descriptor = descriptor_for_context(context, root)
        try:
            ingest_stage(
                descriptor["pipeline_run_id"],
                descriptor["input_name"],
                Path(descriptor["output_dir"]),
            )
        except Exception:
            run_dir = Path(descriptor["output_dir"]) / descriptor["pipeline_run_id"]
            if run_dir.is_dir():
                record_task_attempt(descriptor, "ingest", _try_number(context), "failed")
            raise
        record_task_attempt(descriptor, "ingest", _try_number(context), "success")
        return descriptor

    @task(task_id="transform", retries=0)
    def transform(descriptor: dict[str, str]) -> dict[str, str]:
        from incident_pipeline.airflow_runtime import record_task_attempt
        from incident_pipeline.pipeline import transform_stage

        context = get_current_context()
        try:
            transform_stage(
                descriptor["pipeline_run_id"],
                Path(descriptor["output_dir"]),
                raise_on_failure=True,
            )
        except Exception:
            record_task_attempt(descriptor, "transform", _try_number(context), "failed")
            raise
        record_task_attempt(descriptor, "transform", _try_number(context), "success")
        return descriptor

    @task(task_id="validate", retries=0)
    def validate(descriptor: dict[str, str]) -> None:
        from incident_pipeline.airflow_runtime import record_task_attempt
        from incident_pipeline.pipeline import validate_stage

        context = get_current_context()
        try:
            validate_stage(
                descriptor["pipeline_run_id"],
                Path(descriptor["output_dir"]),
                raise_on_failure=True,
            )
        except Exception:
            record_task_attempt(descriptor, "validate", _try_number(context), "failed")
            raise
        record_task_attempt(descriptor, "validate", _try_number(context), "success")

    validate(transform(ingest()))


orders_daily_pipeline_dag = orders_daily_pipeline()

