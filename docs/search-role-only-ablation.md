# Retrieval roles without generated query text

Hypothesis: a Planner navigation query can drift toward advice while its role still represents a useful retrieval purpose. Jev should assess the original user question and the source evidence. This experiment retains retrieval roles but omits generated query strings from `retrieval_objectives`. Temporal verification queries remain intact because they define explicit temporal operators.

Independent English, Chinese and Spanish cases cover availability, pet rules, device compatibility, travel documents and budget. Each compares a material personal condition with a relevant preference, while both carry an advice-oriented navigation hint. Both variants ranked the condition first in 12/12 order-swapped tests. Role-only input widened score margins; this does not itself establish a ranking improvement in a larger pool. Script and records: `/private/tmp/remi-objective-noise.py` and `/private/tmp/remi-objective-noise.json`.

The experiment uses `--role-only-rerank` and preserves production defaults, returned content and all candidate generation. The paired replay freezes the V82 live adjacent-context run's Planner and query clocks on the V76 snapshots. Compare exact candidate pools, RRF scores, rank metrics, lifecycle and latency. Three targeted tests passed for source preservation, temporal operators and frozen planning.
