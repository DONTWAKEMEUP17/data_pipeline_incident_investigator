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

## Error-review checkpoint / 错误复盘 checkpoint

Choose two failed **development** cases and explain what evidence the agent needed or how its decision policy should change. Good candidates are:

- `eval_d006` — null amount
- `eval_d007` — unknown customer reference
- `eval_d010` — stale input date

请选择两个失败的 development cases，并提出一个 prompt 或 tool-policy change。先不要根据 held-out errors 调参；否则 held-out set 就不再衡量 generalization。

## Reproduce / 复现

```sh
.venv/bin/python -m incident_pipeline.evaluation_cases
.venv/bin/python -m incident_pipeline.evaluate
.venv/bin/python -m unittest discover -s tests -v
```

The generated case artifacts are in `evaluation/artifacts/`, labels are in `evaluation/cases.json`, and the initial saved result is `evaluation/results/local_initial.json`.
