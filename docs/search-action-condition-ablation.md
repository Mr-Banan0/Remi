# Action-condition scoring experiment

The experiment activates when the complete candidate set includes a constraint retrieval role. It uses a focused feasibility-condition question for primary evidence, preserving temporal verification questions, candidate generation, source context and the external result format. Requests without constraint roles retain the production scoring question. Each rank call uses its own shallow ranker copy; the normal scorer and subsequent requests remain unchanged. It uses the same number of provider calls and judgments.

Independent synthetic tests cover evening commitments, ended restrictions, committed funds, attribution, hypothetical statements, direct facts, Chinese and Spanish requests. The normal scorer and a broader decision-impact rewrite pass 6/8; the focused condition question passes 8/8. These pairs do not establish full-ranking quality. A direct-fact case is included as a diagnostic, while actual requests without constraint roles retain the normal scorer.

Seven offline tests pass, covering temporal operators, input immutability, subsequent direct calls, primary/context presentation and context-limit recovery. Production remains V84 until paired full Search results justify promotion. Evidence files: `/private/tmp/remi-decision-impact-check.json` and `/private/tmp/remi-action-condition-check.json`.

## Fixed-Planner complete comparison

On the V83 bank with all Planner outputs/clocks frozen from `search-v84-live-v83-20261102-r2`, `search-v84-condition-v83-paired-20261102-r1` scores R@10 96.5812%, MRR@10 92.3077%, R@100 97.4359%; lifecycle passes 3/3 and all requests succeed. The matching control `search-v84-authority-control-v83-20261102-r1` scores R@10 92.7350%, MRR@10 85.3846%, R@100 97.4359%. Candidate IDs and base scores match across all 16 queries. The previously unstable action query improves from first-gold rank 10 to rank 1.

This isolates a ranking benefit for one fixed plan. Live-Planner runs on the V83 and V84 query-boundary banks are required to establish transfer and stability.
