# Action-condition scoring experiment

The experiment activates when the complete candidate set includes a constraint retrieval role. It uses a focused feasibility-condition question for primary evidence, preserving temporal verification questions, candidate generation, source context and the external result format. Requests without constraint roles retain the production scoring question. Each rank call uses its own shallow ranker copy; the normal scorer and subsequent requests remain unchanged. It uses the same number of provider calls and judgments.

Independent synthetic tests cover evening commitments, ended restrictions, committed funds, attribution, hypothetical statements, direct facts, Chinese and Spanish requests. The normal scorer and a broader decision-impact rewrite pass 6/8; the focused condition question passes 8/8. These pairs do not establish full-ranking quality. A direct-fact case is included as a diagnostic, while actual requests without constraint roles retain the normal scorer.

Seven offline tests pass, covering temporal operators, input immutability, subsequent direct calls, primary/context presentation and context-limit recovery. Production remains V84 until paired full Search results justify promotion. Evidence files: `/private/tmp/remi-decision-impact-check.json` and `/private/tmp/remi-action-condition-check.json`.
