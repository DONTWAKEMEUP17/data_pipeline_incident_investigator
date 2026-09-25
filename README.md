# Data Pipeline Incident Investigator — Local Prototype

A product-minded local prototype for investigating failed data pipelines from saved evidence. It combines a deterministic CSV-to-DuckDB pipeline, bounded agent tools, shortcut-resistant evaluation, a real local Airflow adapter, human-readable reports, and a browser-based review queue. The project uses synthetic data and defaults to an offline deterministic adapter; OpenAI evaluation is explicit and optional.

## What this demonstrates

- A failure callback can remain thin while a separate investigator performs bounded evidence gathering.
- Agent reports can be validated against real tool references and abstain when evidence or budget is insufficient.
- Schema drift and duplicate-ID failures travel through distinct Airflow stages and produce distinct diagnoses.
- Human feedback can be collected separately without rewriting incident evidence or implying that a pipeline was repaired.
- A fixed heldout benchmark can expose category-policy errors while preserving strong cause and evidence scores.

See [the architecture and acceptance criteria](docs/architecture.md) for trust boundaries, the two execution paths, verified results, and current limits.

## Quick product demo

After creating the virtual environment and installing `requirements.txt`, run:

```sh
.venv/bin/python -m incident_pipeline.demo
```

Open `http://127.0.0.1:8765`. The command builds an isolated `demo_workspace/` with one healthy run and two failed runs, creates grounded Schema drift and Data quality reports, and starts the local review UI. Press `Ctrl+C` to stop. Re-running rebuilds the same fixed runs without accumulating incidents; feedback remains in the separate demo SQLite database.

This fast path does not launch Airflow. It reuses the same pipeline stages, failure-event contract, evidence provider, investigator, report renderer, and web app. Run `bash scripts/airflow_m4b_smoke.sh` for the real Docker Airflow integration proof.

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
.venv/bin/python -m incident_pipeline.evaluate
.venv/bin/python -m unittest discover -s tests -v
```

The generator accepts a different output directory via `--output-dir`. It regenerates the four known runs and overwrites only their known generated files. It needs no API key. The CSV fixtures are made-up orders, not personal data.

Run the same offline acceptance path used by CI with:

```sh
bash scripts/verify_local.sh
```

This runs the full unit suite, compiles the Python modules, builds the isolated product demo, and checks the Git diff for whitespace errors. Docker Airflow smoke tests remain separate manual integration checks.

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

The saved run directory is the evidence boundary. `LocalArtifactProvider` implements five typed evidence methods: `get_run_summary`, `read_stage_log`, `compare_schema`, `profile_table`, and `sample_rows`, plus one explicitly enabled sandbox remediation verifier. Keeping observations on disk makes a failure reproducible without running the pipeline again. `AirflowEvidenceProvider` implements the same evidence interface, so the investigator and report renderer remain independent of orchestration.

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

Shared request, observation, and report types live in `incident_pipeline/contracts.py`. Keeping them outside the executable CLI module prevents Python from creating incompatible class identities when `agent.py` runs as `__main__` while an adapter is imported by package name.

The included `DeterministicLearningAdapter` chooses based on returned observations. It takes different paths for schema drift, duplicate IDs, healthy runs, and empty batches. This makes the observe → decide → act loop inspectable and testable without credentials or cost. It does not prove that an LLM will behave correctly. Unit tests use mocked adapter responses to test invalid citations, output validation, and budget exhaustion.

Each investigation allows at most five tool calls, six decision steps, and 4,000 serialized characters per observation. Tool-level row, line, column, and cell bounds still apply. An invalid evidence citation, tool error, or exhausted budget produces a partial `unknown` report rather than an invented cause. The command prints the report and trace to stdout and does not save or modify pipeline data.

### Optional OpenAI adapter

The OpenAI Responses API adapter is implemented in `incident_pipeline/openai_adapter.py`. It requires an explicit model choice and reads the API key from the standard environment variable. Do not put the key in this repository or paste it into logs.

```sh
export OPENAI_API_KEY='set-this-locally'
export OPENAI_MODEL='your-chosen-model-id'
.venv/bin/python -m incident_pipeline.agent failed_001 \
  --adapter openai --max-api-calls 4 --max-output-tokens 700 --reasoning-effort low
```

Only bounded synthetic observations are sent. The request uses `store=False`, allows at most six API calls in code, caps each response at 1,000 output tokens in code, and caps serialized model input at 24,000 characters. The defaults are four API calls, 700 output tokens per call, and low reasoning effort. The last allowed API call uses a final-report-only schema, so a model cannot spend the final decision on another tool request. Tool parameter bounds are also encoded in the model schema; for example, `sample_rows.limit` is 1–5. The result records actual API call, input-token, and output-token counts. `model_cost_usd` remains `null` because exact dollars depend on the chosen model's current pricing; use the Platform usage controls as the account-level dollar guardrail.

Tests inject a fake SDK client and make no network calls. The default CLI adapter remains the free deterministic local adapter.

The decision output intentionally uses one flat Pydantic object. A discriminated union generated the unsupported JSON Schema keyword `oneOf` during the first real integration attempt. The regression test now examines the exact strict schema generated by the installed OpenAI SDK: the root is an object, every property is required, `additionalProperties` is false, and no `oneOf` keyword appears. Nullable unused fields generate supported `anyOf` entries.

## Reproducible evaluation

Milestone 3 adds 24 fixed synthetic cases: four variants for each of the four failure families, four healthy runs, and four ambiguous runs. Sixteen cases are development cases and eight are held out, including at least one unseen variant from every family. The fixed seed is `20260922`. Expected labels live in `evaluation/cases.json`; they are used by the scorer and are never passed to the evidence provider or agent.

```sh
# Regenerate all cases and the label manifest.
.venv/bin/python -m incident_pipeline.evaluation_cases

# Run the baseline and free deterministic agent on identical cases.
.venv/bin/python -m incident_pipeline.evaluate

# Re-run one development error for inspection.
.venv/bin/python -m incident_pipeline.evaluate \
  --case eval_d007 \
  --output evaluation/results/eval_d007.json
```

The saved initial result is `evaluation/results/local_initial.json`. The measured result is intentionally unflattering to the agent: baseline category accuracy is `100%` and deterministic-agent category accuracy is `58.33%` across 24 cases. Both have `100%` evidence validity. The current agent handles schema drift, duplicate IDs, healthy runs, and ambiguous abstention, but returns `unknown` for all join/reference and freshness/volume cases and for two data-quality variants. The baseline wins because the generated validation-check names are highly descriptive and the evaluator uses a documented fixed mapping from its first symptom to a category.

The initial Terra development run scored `87.5%` category accuracy and `75%` appropriate behavior. After adding a deterministic healthy-run guardrail and clarifying abstention in the prompt, a fresh 16-case development run scored `93.75%` on both metrics with `100%` evidence validity. One malformed tool request remains. These are development results; the held-out Terra evaluation has not been run. See `evaluation/README.md` and `evaluation/results/openai_development_after_guardrails.json` for the complete comparison.

Benchmark v2 adds a separate shortcut-resistant suite rather than rewriting these v1 results. Its 18 cases use opaque validation IDs shared across different cause families, add concrete cause and evidence-sufficiency scoring, and default to the 12 development cases. The first free local development run scores the symptom baseline at `33.33%` category accuracy and `0%` cause accuracy; the deterministic agent scores `33.33%` category accuracy and `33.33%` cause accuracy. See `evaluation/v2/README.md` for the complete chronology and frozen evaluation.

An initial unchanged-prompt Terra sample over four v2 development cases scored `50%` category accuracy, `75%` cause accuracy, and `75%` evidence sufficiency. Review showed one tool-selection miss and one ambiguous category boundary despite a correct causal explanation. All four cases used the complete four-call budget. The snapshot was preserved without reruns, and heldout was still unused at that stage.

After clarifying category boundaries, tool choice for opaque checks, and evidence-triggered stopping, the same four-case sample reached `100%` category accuracy and evidence sufficiency while using 14 rather than 16 API calls. The full 12-case v2 development result is `91.67%` category accuracy, `83.33%` automated cause accuracy, and `100%` evidence validity across 38 API calls. Manual review identified one category-policy disagreement and several false negatives from the keyword/tool-based scorers. At that stage, heldout remained unused while those scoring rules were reviewed.

Scorer revision `content-entailment-v2.1` evaluates only cited evidence content and accepts multiple deterministic wording and evidence paths. Rescoring the saved traces requires no API calls and gives the full Terra development run `100%` cause accuracy and `100%` evidence sufficiency; category accuracy remains `91.67%` because the original `eval2_d008` taxonomy disagreement is preserved.

After a final development-only taxonomy clarification, prompt fingerprint `f3673a8d69f1` and scorer `content-entailment-v2.1` were frozen. The six v2 heldout cases were then run exactly once: Terra achieved `83.33%` category accuracy, `100%` cause accuracy, `100%` evidence validity, and `100%` evidence sufficiency with 19 API calls. The single category miss is retained without post-heldout tuning. The result is `evaluation/v2/results/openai_heldout_frozen.json`.

## Sandboxed remediation proposal

The first remediation prototype handles the synthetic `eval2_d008` customer-key normalization mismatch. It opens the saved run database read-only, loads the synthetic customer reference, and tests bounded `TRIM` and `UPPER(TRIM(...))` candidates in an in-memory DuckDB sandbox.

A candidate is marked verified only when it resolves all unmatched keys, preserves row and null counts, creates no empty keys, creates no source-key normalization collisions, and uses a unique reference that is already canonical under the candidate. The report includes assumptions, a changed-row preview, every verification count, and `changes_made: false`.

```sh
.venv/bin/python -m incident_pipeline.remediation eval2_d008
```

The saved proposal is `evaluation/v2/remediation/eval2_d008.json`. For the synthetic case, `UPPER(TRIM(customer_id))` changes `c-101` to `C-101`, reduces unmatched keys from one to zero, keeps three rows, and creates no collision. A human still confirms that customer IDs are semantically case-insensitive before changing the real pipeline contract.

The same verifier is available to the OpenAI investigator as an opt-in typed tool. It uses a separate prompt revision, requires the summary, validation log, and `orders_daily` row evidence first, and still runs only the fixed sandbox candidates:

```sh
.venv/bin/python -m incident_pipeline.agent eval2_d008 \
  --artifacts-dir evaluation/v2/artifacts \
  --adapter openai --model gpt-5.6-terra \
  --max-api-calls 5 --max-output-tokens 700 --reasoning-effort low \
  --enable-remediation
```

The report may cite a verified candidate and request human approval. The tool cannot apply the candidate, write to the saved DuckDB database, or edit the source CSV.

## Local Airflow DAG — Milestone 4A

The same pipeline stages now run as the Airflow 3 TaskFlow DAG `orders_daily_pipeline`. Airflow is isolated in a pinned Docker image because the project's Python 3.9 environment is older than the Python versions supported by Airflow 3.3.2.

```sh
bash scripts/airflow_m4a_smoke.sh
```

The smoke run executes one healthy scenario and two distinct failures. The schema-drift run fails at transform; the duplicate-ID run has the correct schema, completes transform, and fails the uniqueness check at validate. Each failed task writes its bounded pipeline evidence before raising the task exception. See `airflow/README.md` and `airflow/results/m4a_verification.json`.

## Airflow failure handoff — Milestone 4B

Each executed task has a thin failure callback. It writes only the DAG ID, Airflow run ID, task ID, attempt number, schema version, and a deterministic idempotency key. It does not call a model or run the investigator. A separate local process consumes that event, maps it to saved artifacts through `AirflowEvidenceProvider`, runs the existing bounded investigator, and renders one Markdown report.

```sh
bash scripts/airflow_m4b_smoke.sh
```

The smoke run creates one schema-drift incident and one duplicate-ID data-quality incident. It intentionally processes the events twice and verifies that each failed task produces one event and one report. It uses the offline deterministic adapter, makes zero model API calls, and makes no pipeline changes. Generated events and reports stay under ignored `airflow_incidents/`; the normalized verification result is checked in at `airflow/results/m4b_verification.json`.

## Read-only incident view — Milestone 5A

After generating at least one Airflow incident, start the local product view:

```sh
.venv/bin/python -m incident_pipeline.incident_web
```

Open `http://127.0.0.1:8765`. The landing page lists completed investigations newest first. Each detail page shows the diagnosis, proposed human action, cited evidence, Airflow task identity, and a link to the original Markdown report.

The server uses only the Python standard library and reads canonical files under `airflow_incidents/incidents`. It escapes artifact text before rendering, rejects unsafe or oversized artifacts, and cannot change pipeline data or incident output.

## Local human feedback — Milestone 5B

Each incident page includes three review signals: `useful`, `incorrect`, and `uncertain`. Selecting one writes a single current value for that event to `airflow_incidents/feedback.sqlite3`; selecting again updates the same row. The incident list shows the current signal and aggregate counts.

The root-cause label is descriptive metadata, while the colored human-review state answers whether the report helped: green means useful, red means incorrect, yellow means uncertain, and gray means not reviewed. A useful review does not imply that the underlying pipeline has been repaired. The list can be filtered by all review states; `Needs review` includes awaiting, incorrect, and uncertain reports, while useful reports are treated as review-complete.

Feedback is kept in a dedicated local SQLite database and contains only the event ID, selected value, and update timestamp. It does not alter the incident report, pipeline artifacts, Airflow metadata, or source data. The feedback form is size-bounded, uses a per-process CSRF token, accepts only the three allowlisted values, and is the only write route exposed by the local server.

## Human-readable incident report

`incident_pipeline.report` turns saved agent or evaluation JSON into deterministic Markdown. It validates that every cited evidence reference exists in the trace, summarizes the bounded tool results, and optionally attaches a sandbox remediation proposal. Rendering makes no model calls.

```sh
.venv/bin/python -m incident_pipeline.report \
  evaluation/v2/results/openai_eval2_d008_after_taxonomy.json \
  --run-id eval2_d008 \
  --remediation-json evaluation/v2/remediation/eval2_d008.json \
  --output reports/eval2_d008.md
```

The checked-in example is `reports/eval2_d008.md`. It is intended for the data engineer or analyst handling the incident and presents the diagnosis, cited evidence, recommended human action, verified candidate, safety checks, assumptions, and change preview. Raw sampled rows are omitted from the rendered evidence detail; the grounded claim and bounded tool reference remain visible.

Agent-development metadata stays in the original JSON trace. Pass `--include-debug` only when a developer needs model-step counters and the complete tool execution trail in a technical appendix.

Latency is measured with `perf_counter` and varies by machine. The deterministic adapter has zero API calls and zero token cost. These numbers measure this synthetic benchmark only; they do not establish production accuracy or an LLM improvement. See `evaluation/README.md` for metric definitions, family slices, limitations, and the human review checkpoint.

OpenAI evaluation is explicit and opt-in. Start with one development case because each selected case can use up to the configured API-call budget:

```sh
.venv/bin/python -m incident_pipeline.evaluate \
  --adapter openai --model gpt-5.6-terra \
  --case eval_d007 \
  --max-api-calls 4 --max-output-tokens 700 --reasoning-effort low \
  --confirm-paid-evaluation \
  --output evaluation/results/openai_eval_d007.json
```

Do not tune from held-out errors. After reviewing two failed **development** cases, choose a prompt or tool-policy hypothesis, change it using only development cases, and then run the held-out split once:

```sh
.venv/bin/python -m incident_pipeline.evaluate \
  --split heldout \
  --output evaluation/results/local_heldout_after_change.json
```
