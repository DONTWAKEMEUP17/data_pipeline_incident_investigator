# Benchmark v2 / 第二版评估

## Purpose / 目的

Benchmark v1 is useful for testing the agent loop, but descriptive check names such as `order_ids_unique` let a fixed mapping infer the broad category. V2 removes that shortcut. Validation checks use neutral identifiers such as `gate_02` and `gate_17`, and the same `gate_17` appears in join/reference, freshness/volume, and ambiguous cases.

Benchmark v1 适合验证 agent loop，但 `order_ids_unique` 这类名字几乎直接透露 category。V2 使用 `gate_02`、`gate_17` 等中性名称，而且同一个 `gate_17` 会出现在不同 failure family 中。

V2 contains 18 deterministic synthetic cases with seed `20260923`: 12 development cases and 6 heldout cases. Each broad family has a heldout case. Labels and scoring rules live in `cases.json` and Python case specifications; they are never included in a run artifact or agent observation.

V2 有 18 个固定合成案例：12 个 development、6 个 heldout。标签只供 scorer 使用，不会传给 evidence provider 或 agent。

## What changed / 改了什么

- **Neutral check names:** a check ID no longer maps directly to a category.
- **Shared symptoms:** similar failed gates can have different causes.
- **Cause labels:** each diagnosable case has a concrete expected cause such as `duplicate_order_id` or `stale_reference_snapshot`.
- **Required evidence:** each cause lists the tool outputs needed to support it, such as `profile_table` or `read_stage_log` plus `sample_rows`.
- **Heldout lock:** the evaluator defaults to development and requires `--unlock-heldout` before it will run heldout cases.

- **中性 check name**：不能只靠名字分类。
- **共享 symptom**：相同 failed gate 可以来自不同原因。
- **具体 cause label**：区分 broad category 与 underlying cause。
- **必要 evidence**：报告必须引用真正支持原因的工具结果。
- **Heldout lock**：默认只运行 development；heldout 需要显式解锁。

## Metrics / 指标

- **Category accuracy:** broad report category matches the label.
- **Cause accuracy:** the report explanation contains all required cause concepts. Each concept may have several accepted phrases.
- **Evidence validity:** every cited reference came from an actual tool result.
- **Evidence sufficiency:** cited tool results include the evidence types designated as decisive for that case.
- **Appropriate behavior:** diagnosed cases use the right category, healthy cases use `unknown` with low uncertainty, and ambiguous cases abstain with high uncertainty.

Cause accuracy is a deterministic keyword heuristic, not a semantic judge. It is inspectable and reproducible, but paraphrases outside the accepted term groups may be marked wrong. A later evaluator can replace this scorer without changing the artifacts.

Cause accuracy 使用可复现的 keyword heuristic，并不是完整 semantic judge。它的优点是透明，限制是某些合理的同义改写可能被判错。

## Local development result / 本地 development 结果

No API was called. The saved result contains only the 12 development cases.

| Metric | Symptom baseline | Deterministic agent |
| --- | ---: | ---: |
| Category accuracy | 33.33% | 33.33% |
| Cause accuracy | 0% | 33.33% |
| Evidence validity | 100% | 100% |
| Evidence sufficiency | 25% | 41.67% |
| Appropriate behavior | 16.67% | 33.33% |

The lower category scores are expected: generic check IDs break the v1 name-to-category mapping. The deterministic agent was designed for the original named checks, so this result is a pre-tuning baseline rather than a product-quality score.

低分是预期结果。它说明 v1 shortcut 已失效，也为之后使用 development cases 改进 investigation policy 留下了诚实的 baseline。

## Reproduce / 复现

```sh
# Generate all deterministic artifacts and the external label manifest.
.venv/bin/python -m incident_pipeline.evaluation_cases_v2

# Evaluate only the 12 development cases with no API calls.
.venv/bin/python -m incident_pipeline.evaluate_v2

# Inspect one development case with Terra. This is paid and explicit.
.venv/bin/python -m incident_pipeline.evaluate_v2 \
  --adapter openai --model gpt-5.6-terra \
  --case eval2_d003 \
  --max-api-calls 4 --max-output-tokens 700 --reasoning-effort low \
  --confirm-paid-evaluation \
  --output evaluation/v2/results/openai_eval2_d003.json

# Run all tests locally.
.venv/bin/python -m unittest discover -s tests -v
```

Do not use `--unlock-heldout` while tuning. The six heldout cases have been generated, but neither Terra nor the local v2 evaluation result has run them.

调参期间不要使用 `--unlock-heldout`。当前保存的 v2 result 只包含 development cases，尚未对 v2 heldout 运行 Terra 或本地 evaluator。
