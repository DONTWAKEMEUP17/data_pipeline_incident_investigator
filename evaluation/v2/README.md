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

## Initial Terra development sample / Terra 初始样本

The unchanged prompt fingerprint `12655904bc19` was run once on four representative development cases: `eval2_d001`, `eval2_d003`, `eval2_d007`, and `eval2_d012`. Every case used the full four-call budget. No case was rerun, and no heldout case was evaluated.

使用未修改的 prompt 对四个 development cases 各运行一次。每个 case 都使用了完整的四次 API-call budget；没有重跑错误，也没有运行 heldout。

| Metric | Symptom baseline | Terra agent |
| --- | ---: | ---: |
| Category accuracy | 50% | 50% |
| Cause accuracy | 0% | 75% |
| Evidence validity | 100% | 100% |
| Evidence sufficiency | 50% | 75% |
| Appropriate behavior | 25% | 50% |
| Average tool calls | 2 | 3 |
| Average latency | 0.968 ms | 12.173 s |
| Model API calls | 0 | 16 |
| Input tokens | 0 | 15,427 |
| Output tokens | 0 | 2,462 |

Per-case review:

- `eval2_d001`: correctly diagnosed the renamed amount column as schema drift. It also spent one call profiling the raw table after the primary blocker was already established.
- `eval2_d003`: correctly abstained from guessing what opaque `gate_03` meant, but chose schema comparison instead of profiling the output table and therefore missed the duplicate order ID.
- `eval2_d007`: correctly explained and cited the stale customer-reference snapshot, but selected `freshness_volume` while the benchmark labels a stale reference used by a key lookup as `join_reference`. This is a taxonomy-boundary ambiguity rather than a failed causal explanation.
- `eval2_d012`: correctly abstained with high uncertainty when two timestamp monitors conflicted. It also used a final profile call that did not change the diagnosis.

The sample does not yet justify a general router. It points first to a narrower tool-selection policy for opaque validation failures, a clearer category definition for stale reference data, and a stopping policy that avoids an extra anomaly-search call when the report is already grounded.

这个样本暂时不能证明需要完整 router。更直接的问题是 tool selection、category taxonomy 和 stopping policy。

The exact unchanged snapshot is `results/openai_development_sample_initial.json`. Exact dollar cost remains `null`; the adapter records calls and tokens without hard-coding model pricing.

## After taxonomy, tool-policy, and stopping changes / Policy 修改后

Prompt fingerprint `a1d6208621a5` introduced three development-only policy changes:

1. Stale reference data used by a lookup belongs to `join_reference`; `freshness_volume` is reserved for the primary input batch.
2. After a successful transform and opaque validation failure, read the validation log and then profile `orders_daily` when the log is inconclusive. Compare schema only when transform failed or evidence suggests a contract mismatch.
3. Finish when the primary cause is grounded. Remaining budget alone does not justify searching for an additional anomaly.

修改只涉及 prompt policy；v2 artifacts、labels 和 heldout cases 没有改变。

The same four-case sample was run once after the change:

| Metric | Before | After |
| --- | ---: | ---: |
| Category accuracy | 50% | 100% |
| Automated cause accuracy | 75% | 75% |
| Evidence validity | 100% | 100% |
| Automated evidence sufficiency | 75% | 100% |
| Appropriate behavior | 50% | 100% |
| Average tool calls | 3 | 2.5 |
| Average latency | 12.173 s | 8.163 s |
| Model API calls | 16 | 14 |
| Input tokens | 15,427 | 15,265 |
| Output tokens | 2,462 | 1,717 |

The tool-selection miss on `eval2_d003` was fixed. `eval2_d001` and `eval2_d007` now stop after two tool calls. `eval2_d012` remained correct but still used a final profile call. The automated cause scorer kept `eval2_d007` at false because the explanation gave the old reference date and missing refresh without using one of the scorer's accepted words such as “stale.” Manual review finds the cause grounded; the saved automated score was not rewritten after seeing the answer.

Because the selected sample improved, the remaining eight development cases were run once and merged with it. No selected case was charged twice.

| Full development metric | Symptom baseline | Terra agent |
| --- | ---: | ---: |
| Category accuracy | 33.33% | 91.67% |
| Automated cause accuracy | 0% | 83.33% |
| Evidence validity | 100% | 100% |
| Automated evidence sufficiency | 25% | 66.67% |
| Appropriate behavior | 16.67% | 91.67% |
| Average tool calls | 1.917 | 2.25 |
| Average latency | 1.063 ms | 6.765 s |
| Model API calls | 0 | 38 |
| Input tokens | 0 | 41,300 |
| Output tokens | 0 | 4,636 |

The only category/behavior error is `eval2_d008`: Terra described lowercase `c-101` as a primary-batch format violation and selected `data_quality`; the chosen benchmark taxonomy treats a customer-key comparison failure as `join_reference`.

Manual review also found that the current evidence-sufficiency metric is too tool-specific. Four reports cited evidence that already contained the exact schema order, invalid amount value, input dates, or row-count threshold, but the scorer required an extra prescribed tool. Similarly, the automated cause scorer missed date-based explanations that did not contain an accepted adjective. These raw deterministic scores remain preserved. Before heldout evaluation, the scorer should be revised on development data to accept alternative evidence paths and then versioned separately.

自动指标保留原始结果，没有为了提高分数而重写。人工检查认为 12 个报告的具体 cause 都有依据；真正剩余的 policy disagreement 是 `eval2_d008` 的单标签 taxonomy。

Saved snapshots:

- `results/openai_development_sample_after_policy.json`
- `results/openai_development_remaining_after_policy.json`
- `results/openai_development_after_policy.json`

Reproduce the merge without API calls:

```sh
.venv/bin/python -m incident_pipeline.merge_evaluations_v2 \
  evaluation/v2/results/openai_development_sample_after_policy.json \
  evaluation/v2/results/openai_development_remaining_after_policy.json \
  --output evaluation/v2/results/openai_development_after_policy.json
```

V2 heldout has not been evaluated. Exact dollar cost remains `null` because pricing is not hard-coded.
