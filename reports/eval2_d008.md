# Incident Report: eval2_d008

> **Status:** Diagnosis complete · Human approval required · No changes made

## Overview

| Field | Value |
| --- | --- |
| Run ID | `eval2_d008` |
| Root-cause category | Join / reference |
| Uncertainty | Low |
| Changes made | No |

## What happened

The primary blocker is gate_17: one source customer key fails a case-sensitive comparison to canonical uppercase customer keys. The sampled batch identifies customer_id "c-101" as the mismatched value, while the other sampled customer IDs are uppercase. This is a casing-only mismatch against the reference representation, so it is a reference join/key-normalization failure rather than independently invalid primary data.

## Evidence

1. **`tool-1` · `get_run_summary`**
   - Finding: Validation failed solely at gate_17, with one failing record; ingest and transform both succeeded for 3 rows.
   - Observed: Run status=failed; ingest=success (3 rows), transform=success (3 rows), validate=failed (3 rows). Failed checks: gate_17.
2. **`tool-2` · `read_stage_log`**
   - Finding: gate_17 performs a case-sensitive comparison against canonical uppercase customer keys and reports one nonmatching source value.
   - Observed: L1: Completed 4 opaque checks; status=failed. L2: gate_17 used a case-sensitive comparison against canonical uppercase customer keys. L3: One source value did not match; inspect row values before changing the reference.
3. **`tool-3` · `sample_rows`**
   - Finding: The sampled rows show customer_id "c-101" in lowercase, whereas the other customer IDs use uppercase C.
   - Observed: Inspected 3 bounded rows from orders_daily; raw rows are omitted from this report.

## Recommended human action

Review the canonical customer-key join/normalization policy and align the source key "c-101" with its canonical uppercase representation before validation; confirm the corresponding reference customer key exists. No fix was run.

## Sandboxed remediation

| Field | Value |
| --- | --- |
| Status | verified_candidate |
| Candidate | `upper_trim` |
| Checks passed | Yes |
| Approval required | Yes |
| Sandbox only | Yes |
| Changes made | No |

**Candidate change:** Apply UPPER(TRIM(customer_id)) to the source customer_id before the reference lookup.

The sandbox found 1 unmatched source key and reduced it to 0 using upper_trim, without changing row count or creating normalization collisions.

### Verification checks

| Check | Before | After |
| --- | ---: | ---: |
| Row count | 3 | 3 |
| Unmatched customer keys | 1 | 0 |
| Null customer keys | 0 | 0 |
| Changed rows | — | 1 |
| Normalization collisions | — | 0 |
| Reference duplicate keys | — | 0 |
| Reference noncanonical keys | — | 0 |

### Change preview

| Order ID | Before | After |
| --- | --- | --- |
| `O-201` | `c-101` | `C-101` |

### Assumptions requiring review

- The supplied customer reference is authoritative for this run.
- Customer IDs are case-insensitive and surrounding whitespace is not meaningful.

**Remediation handoff:** Confirm the customer-ID normalization contract with the source and reference owners. If the assumptions are correct, add the verified normalization before the lookup and re-run the full pipeline validation.

---

Generated deterministically from saved investigation artifacts. Rendering made no model calls and changed no pipeline data.
