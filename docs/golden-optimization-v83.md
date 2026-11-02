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
