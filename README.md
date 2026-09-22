# Data Pipeline Incident Investigator — Milestones 0–3

This is a local learning prototype. It generates synthetic CSV-to-DuckDB batch runs, offers bounded read-only evidence tools and a simple failure baseline, runs a minimal bounded investigation loop, and evaluates both systems on the same 24 fixed cases. The default decision adapter is deterministic local code used to learn the agent mechanics; an optional OpenAI adapter is available. There is no Airflow integration, report UI, or external data connection yet.

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

## Learning materials / 学习材料

- `lessons/0001-how-agent-py-works.html` is a short bilingual interactive lesson tied to this code.
- `reference/agent-loop-cheat-sheet.html` is a printable agent-loop reference.
- `MISSION.md` records the learning goal, and `learning-records/` records demonstrated understanding.

### Hands-on checkpoint

The chosen policy is **continue within budget**: after finding the primary blocker, the investigator may spend a remaining bounded call to look for additional anomalies. Run `.venv/bin/python -m incident_pipeline.agent failed_001` and walk through the trace. Explain why `compare_schema` follows `get_run_summary`, why `profile_table` follows the schema mismatch, and why the final report does not claim that the duplicate ID caused the transform failure.
