# Milestone 3 evaluation / 评估说明

## Case design / 案例设计

The benchmark contains 24 deterministic, synthetic runs generated with seed `20260922`. It has four variants for each failure family (`schema_drift`, `data_quality`, `join_reference`, and `freshness_volume`), four healthy runs, and four ambiguous runs. Sixteen are development cases and eight are held out. Every failure family has a held-out variant.

该 benchmark 包含 24 个固定的合成 run，seed 为 `20260922`。四种 failure family 各有四个变体，另有四个 healthy 和四个 ambiguous run；16 个用于 development，8 个用于 held-out。

`cases.json` contains scoring labels. Those labels are outside each run artifact directory and are not passed to `LocalArtifactProvider`, a model adapter, or an agent observation. The agent can see only the same bounded summary, logs, schemas, profiles, and samples available in normal investigations.

`cases.json` 保存评分 labels。它们不在 run artifacts 中，也不会传给 evidence provider、model adapter 或 agent observation。

## Metrics / 指标

- **Category accuracy**: predicted category equals the expected underlying category. Healthy and ambiguous cases expect `unknown`.
- **Evidence validity**: agent citations point to actual tool results; baseline failures include a real bounded log excerpt.
- **Appropriate behavior**: diagnosable failures receive the right non-unknown category, ambiguous cases abstain with high uncertainty, and healthy cases return no cause with low uncertainty.
- **Tool calls, latency, tokens, API calls, cost**: efficiency measurements saved per case and in aggregate. Local latency varies by machine. Exact OpenAI dollar cost remains `null` because the adapter does not hard-code changeable model pricing.

- **Category accuracy**：预测类别是否等于 underlying cause。
- **Evidence validity**：引用是否对应真实 tool result。
- **Appropriate behavior**：该诊断时正确诊断，该 abstain 时明确 abstain。
- **Tool calls / latency / tokens / cost**：记录效率与预算；本机 latency 不能跨机器直接比较。

The baseline does not produce a structured root-cause report. For comparison, the scorer applies a fixed, inspectable mapping from its first failed check or transform log to a category. This mapping is in `incident_pipeline/evaluate.py`. It is part of the baseline definition and is not learned from individual results.

Baseline 本身只报告第一个 failure symptom。评分器使用固定 mapping 把 check/log 转成 category；这个 mapping 是 baseline 定义的一部分，不会按案例临时调整。

## Initial local result / 初始本地结果

| Slice | Cases | Baseline accuracy | Agent accuracy | Agent evidence validity |
| --- | ---: | ---: | ---: | ---: |
| Overall | 24 | 100% | 58.33% | 100% |
| Development | 16 | 100% | 56.25% | 100% |
| Held-out | 8 | 100% | 62.5% | 100% |
| Schema drift | 4 | 100% | 100% | 100% |
| Data quality | 4 | 100% | 50% | 100% |
| Join/reference | 4 | 100% | 0% | 100% |
| Freshness/volume | 4 | 100% | 0% | 100% |
| Healthy | 4 | 100% | 100% | 100% |
| Ambiguous | 4 | 100% | 100% | 100% |

This is a deterministic policy evaluation, not an LLM benchmark. The agent's ten errors are retained in `results/local_initial.json`. The baseline wins because the validation names directly describe these synthetic failures, while the current deterministic agent has explicit branches for only the Milestone 2 scenarios. The result is useful because it identifies coverage gaps before prompt or tool changes.

这是 deterministic policy 的评估，不是 LLM benchmark。Baseline 获胜是因为合成 validation name 很明确，而当前 agent 只实现了 Milestone 2 的少数分支。

## Selected Terra run / Terra 小样本运行

Before changing the prompt or tools, the existing `gpt-5.6-terra` prompt was run on the two development cases selected at the human checkpoint: `eval_d006` (null amount) and `eval_d010` (stale input date).

在修改 prompt 或 tools 之前，使用现有 Terra prompt 运行了用户选择的两个 development cases。

| Metric | Result |
| --- | ---: |
| Category accuracy | 2/2 (100%) |
| Evidence validity | 2/2 (100%) |
| Appropriate behavior | 2/2 (100%) |
| Tool calls | 6 total (3 per case) |
| Model API calls | 8 total (4 per case) |
| Input tokens | 7,040 |
| Output tokens | 1,076 |
| Average latency | 11.387 seconds per case |

`eval_d006` used summary → table profile → transform log → final report. `eval_d010` used summary → validation log → table profile → final report. In both cases, the first two evidence results were already enough to establish the primary category; the third tool mainly ruled out an additional anomaly. This follows the current **continue within budget** policy, but it also identifies a possible latency and token trade-off for later tuning.

The exact result snapshot is saved in `results/openai_eval_d006_eval_d010.json`. It is a two-case development sample, so it does not establish full-benchmark accuracy or improvement over the baseline. No held-out case was used in this Terra run.

完整结果保存在 `results/openai_eval_d006_eval_d010.json`。这是两个 development cases 的小样本，不能代表完整 benchmark，也不能据此声称超过 baseline。

## Full Terra development result / 完整 development 结果

The same unchanged Terra prompt was then run on the remaining 14 development cases. The two compatible result snapshots were validated and merged with `incident_pipeline.merge_evaluations`; the selected two cases were not charged twice. No held-out case was run.

随后使用完全相同的 prompt 运行其余 14 个 development cases，并与之前的两个结果合并。Held-out cases 仍未运行。

| Metric | Baseline | Terra |
| --- | ---: | ---: |
| Cases | 16 | 16 |
| Category accuracy | 100% | 87.5% |
| Evidence validity | 100% | 100% |
| Appropriate behavior | 100% | 75% |
| Average tool calls | 1.875 | 2.75 |
| Average latency | 1.889 ms | 10.429 s |
| Model API calls | 0 | 60 |
| Input tokens | 0 | 52,757 |
| Output tokens | 0 | 8,358 |

Terra correctly classified all 12 diagnosable development failures across schema drift, data quality, join/reference, and freshness/volume. Four behavior errors remain:

- `eval_d013` and `eval_d014` were healthy. After reading the successful summary, Terra requested `compare_schema` without a table argument. The adapter rejected the malformed request and returned a high-uncertainty fallback instead of a low-uncertainty healthy result.
- `eval_d015` was an empty batch with insufficient causal evidence. Terra labeled it `freshness_volume` with medium uncertainty instead of abstaining.
- `eval_d016` had a deliberately uncertain upstream-delivery check. Terra again chose `freshness_volume` with medium uncertainty instead of `unknown` with high uncertainty.

The result supports two prompt hypotheses for development-only tuning: finish immediately with `unknown`/low uncertainty after a fully successful summary, and distinguish a failed volume/freshness symptom from evidence of its underlying cause. No prompt change has been made yet.

结果说明两个可测试的 prompt hypotheses：成功 run 应立即结束；只有 symptom、没有 underlying-cause evidence 时应 abstain。当前尚未修改 prompt。

The complete snapshot is `results/openai_development_initial.json`. Its sources are the two-case and remaining-14-case snapshots. Exact dollar cost is not calculated because model pricing is not hard-coded.

## After healthy guardrail and abstention prompt / 修改后结果

Two development-only changes were selected after reviewing the initial errors:

1. A host-side healthy guardrail returns `unknown` with low uncertainty when the structured summary proves that every stage and validation check succeeded.
2. The prompt now says that zero rows or an explicitly uncertain/conflicting check is a symptom, not a grounded freshness/volume root cause. It requires concrete stale-date or threshold evidence for that category.

选择了两个只针对 development evidence 的修改：healthy shortcut 由 host code 确定执行；ambiguous symptom 必须 abstain，不能直接推断 underlying cause。

| Metric | Before | After | Change |
| --- | ---: | ---: | ---: |
| Category accuracy | 87.5% | 93.75% | +6.25 points |
| Evidence validity | 100% | 100% | unchanged |
| Appropriate behavior | 75% | 93.75% | +18.75 points |
| Average tool calls | 2.75 | 2.688 | -0.062 |
| Average latency | 10.429 s | 9.636 s | -0.793 s |
| Model API calls | 60 | 57 | -3 |
| Input tokens | 52,757 | 53,926 | +1,169 |
| Output tokens | 8,358 | 7,828 | -530 |

Both healthy cases now stop after one summary tool call and one model API call, with low uncertainty. Both ambiguous cases now return `unknown` with high uncertainty. All schema, data-quality, and freshness/volume development cases remained correct.

One join/reference case, `eval_d009`, regressed because Terra requested `compare_schema` without the required `table` after two valid observations. The adapter rejected the malformed request and safely returned an `unknown` fallback. This is retained as a representative model/tool-argument error rather than rerun away.

Post-change prompt fingerprint: `12655904bc19`. The evaluator stores this revision, and the merger rejects snapshots from different revisions. The complete post-change result is `results/openai_development_after_guardrails.json`. No held-out case has been run.

修改后完整结果保存在 `results/openai_development_after_guardrails.json`。Held-out 仍未运行。

## Reproduce / 复现

```sh
.venv/bin/python -m incident_pipeline.evaluation_cases
.venv/bin/python -m incident_pipeline.evaluate
.venv/bin/python -m incident_pipeline.merge_evaluations \
  evaluation/results/openai_eval_d006_eval_d010.json \
  evaluation/results/openai_development_remaining_initial.json \
  --output evaluation/results/openai_development_initial.json
.venv/bin/python -m unittest discover -s tests -v
```

The generated case artifacts are in `evaluation/artifacts/`, labels are in `evaluation/cases.json`, and the initial saved result is `evaluation/results/local_initial.json`.
