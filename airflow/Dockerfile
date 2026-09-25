ARG AIRFLOW_VERSION=3.3.2
FROM apache/airflow:${AIRFLOW_VERSION}-python3.12

COPY airflow/requirements.txt /tmp/incident-pipeline-airflow-requirements.txt
RUN pip install --no-cache-dir -r /tmp/incident-pipeline-airflow-requirements.txt

