#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$repo_dir/airflow/compose.yaml"

cd "$repo_dir"
mkdir -p airflow/runtime airflow_artifacts airflow/results

docker compose -f "$compose_file" build
docker compose -f "$compose_file" run --rm airflow airflow db migrate
docker compose -f "$compose_file" run --rm airflow airflow dags reserialize
docker compose -f "$compose_file" run --rm airflow airflow dags list-import-errors -o json

docker compose -f "$compose_file" run --rm airflow \
  airflow dags test orders_daily_pipeline 2026-09-24T00:00:00+00:00 \
  -c '{"scenario":"healthy"}'

set +e
docker compose -f "$compose_file" run --rm airflow \
  airflow dags test orders_daily_pipeline 2026-09-24T01:00:00+00:00 \
  -c '{"scenario":"schema_drift"}'
failed_exit=$?
set -e

if [[ "$failed_exit" -eq 0 ]]; then
  echo "Expected schema-drift DagRun to fail, but it succeeded." >&2
  exit 1
fi

set +e
docker compose -f "$compose_file" run --rm airflow \
  airflow dags test orders_daily_pipeline 2026-09-24T01:30:00+00:00 \
  -c '{"scenario":"duplicate_id"}'
duplicate_exit=$?
set -e

if [[ "$duplicate_exit" -eq 0 ]]; then
  echo "Expected duplicate-ID DagRun to fail, but it succeeded." >&2
  exit 1
fi

.venv/bin/python scripts/verify_airflow_m4a.py
