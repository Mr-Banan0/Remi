# V85 / remi-0.2.19: focused action-condition scoring

A request with a constraint role in its candidate set asks Jev to score concrete personal conditions that affect whether, when or how the requested action can be carried out. The question includes obligations, limitations, requirements and permissions, including an ended restriction. Other requests keep the primary-statement contribution question from V84. Temporal verification keeps its separate operator. The complete candidate set chooses one question before batching; each request uses a private ranker copy, sharing only the existing transport and diagnostics.

Add, Planner, retrieval, graph expansion, source context, external response and model/call budgets are unchanged. The experiment wrapper and switch were removed because the tested behavior is now the single production path. Historical ablations are reproducible from commit `849ab5f` in `/private/tmp/remi-runtime-v84-condition-r1`.

Independent synthetic scoring passed 8/8 versus the previous 6/8. Fixed-Planner and two live-Planner Search runs on V83/V84 query-boundary banks each achieved R@10 96.5812%, MRR@10 92.3077%, R@100 97.4359%, lifecycle 3/3 and no failed requests. Live mean/P95 latency was 11.61/14.62 and 11.54/14.04 seconds. The fixed comparison has identical candidates/base scores for all 16 queries; its control scores 92.7350%/85.3846% at R@10/MRR@10.

Full offline regression: 523 passed, one existing Starlette deprecation warning. Tests cover consistent scoring across batches, a subsequent direct request, source immutability, temporal scores, source context, provider-size recovery and API version consistency. Frozen production-path Search validation remains required before concluding stability. Fresh Add banks come from completed V83/V84 full runs; this release changes only Search scoring and version metadata.

## Frozen production Search validation

Commit `9d7846c`, runtime fingerprint `cec30c8002bbee2632043eb7547bfd85da63075302abd4e024fb559dc1a75f19`, was evaluated with live Planner/Jev on three query-boundary banks. Each run has R@10 96.5812%, MRR@10 92.3077%, R@100 97.4359%, lifecycle 3/3, and no request/provider errors. All dataset/runtime hashes and interface/model/resource settings match their recorded contracts. Snapshot copies verify each stored SHA256 before loading.

| Bank | Search mean / P95 seconds | Mean returned bytes | Report |
|---|---:|---:|---|
| V83 | 11.58 / 14.79 | 182,088 | search-v85-live-v83-20261102-r1 |
| V84 | 11.42 / 12.97 | 181,775 | search-v85-live-v84-20261102-r1 |
| V76 | 12.09 / 17.30 | 182,308 | search-v85-live-v76-20261102-r1 |

The three banks represent different memory-generation samples for the same 13 Golden questions. This establishes local cross-bank Search repeatability. Relative to the pre-optimization historical maxima, R@10 gains 8.5470 percentage points, MRR@10 6.7308 points and R@100 5.1282 points. The complete V85 fresh-ingestion run is still in progress. Audit artifact: `/private/tmp/remi-v85-three-bank-audit.json`.

Remaining limitations: one action query still lacks the cane evidence in its candidate pool; one broad date/activity query has an evidence item beyond rank 10; first relevant evidence for two queries remains rank 2. Returned evidence volume is substantial and must be included when assessing downstream answer cost. These measurements cover local evidence retrieval and lifecycle behavior.
