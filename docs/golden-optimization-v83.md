# V83 / remi-0.2.17: bounded adjacent evidence context

Search now supplements each selected source package with messages at ordinal distance at most two, within the same owner, session and Add request. The original primary source and cited sources are retained. Existing evidence byte limits and excerpt selection apply to the resulting package. Add, memory updates, graph edges, Planner input and model choices remain unchanged.

The source context index is built once per Search after candidate fusion. It reads the owner's source records once for this stage, filters by the requested historical view, and indexes the selected request groups. Existing graph retrieval has its own source scan. No database schema changes are involved. Trace candidates now expose `evidence_source_ids` for the source blocks actually displayed.

The experimental implementation and `--adjacent-source-context` switch were removed when this behavior became the default. Earlier comparisons remain reproducible from frozen commit `a5defcc` in `/private/tmp/remi-runtime-v82-adjacent-r1`. The primary-source-only scoring and other distinct ablations remain experimental and are not enabled by default.

Evidence for adoption: two fixed-plan V76 bank replays reached R@10 96.58%, with MRR 91.03% and 94.87%. A live-plan replay reached R@10 92.74%, MRR 85.38%, R@100 97.44%, lifecycle 3/3. Source-only repeated fixed-plan R@10 was 82.48%. At ten results, mean output bytes rose from 37,019 to 47,572, while exact facet quotation coverage rose from 82.05% to 97.44%. This is a context-coverage improvement as well as a ranking effect; it is not evidence of a better reranker in isolation.

The private-history Planner prefix combination did not improve MRR (78.46%) and is not part of production. The historical MRR target is 85.58%; live-plan stability and full fresh-ingestion validation remain outstanding. The goal is not complete.

Validation: full regression 517 passed; the extended Search integration test additionally verifies ownership/request boundaries, future source exclusion, primary source preservation, byte budget, a single context-stage source scan, unchanged Planner source IDs and unchanged stored documents.

## New-bank paired replay

`.local-data/search-v83-v81-boundaries-20261102-r1/result.json` uses the completed V81 fresh-ingestion snapshots and freezes that run's Planner and clocks. V83 reached R@10 95.73%, MRR 82.69%, R@100 97.44%; the original V81 run was 86.32%, 76.92%, 93.59%. Fresh-ingestion V83 is still running separately.

Residual top-rank errors include an actual workshop on a different date, a morning preference above a physical constraint, photo-organization advice above a work-device report, and a proposed anecdote above an actual contact record. No explicit temporal-selection promotion caused these four errors. An experimental combination now retains adjacent context in returned evidence while separating primary-source contribution from auxiliary context for Jev. It uses the existing primary-source experiment and leaves production unchanged.

## Completed fresh-ingestion validation

Run `golden-v83-adjacent-20261102-r1/golden-gpt-4o-mini` completed all 47 Add calls, 13 main queries and three lifecycle queries. R@10 is 96.5812%, MRR@10 88.7179%, R@100 97.4359%, and lifecycle passes 3/3. These exceed the recorded historical bests of 88.0342%, 85.5769% and 92.3077%, respectively. This is one full run; repeated live validation remains necessary for a stability claim.

Add mean/P95 latency is 56.98/138.74 seconds; Search mean/P95 is 11.35/14.08 seconds. Average returned evidence volume is 180,516 bytes across 39.54 cards. All requests completed. Diagnostics retain one recovered provider timeout and one repaired invalid model output.

The remaining missing evidence in the last action query exists in the query-boundary source table and has two derived memories, but its source ID is absent from the final candidate pool. The Planner searched current physical limitations and recent training activities. This is a recall coverage gap rather than lost storage or final ranking. A separate misleading-action query has its first gold evidence at rank 5, and the device query at rank 3.
