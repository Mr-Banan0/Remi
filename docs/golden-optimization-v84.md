# V84 / remi-0.2.18: primary-source scoring with adjacent context

Final Search cards retain complete bounded original evidence for returned results. A dedicated Jev view presents the primary source in `original` and auxiliary source blocks in `context`. The ranking instruction evaluates the primary statement's contribution while allowing context to resolve references, speakers and dates. The Planner card format, candidate generation, temporal verification targets and external response content remain unchanged. Source blocks are constructed once per card; the former experimental wrapper repeated this work.

The superseded primary-source experiment module and CLI switch have been removed. Its historical runs remain reproducible from frozen commit `91e36af` at `/private/tmp/remi-runtime-v83-role-r1`. Other ablations remain separate experimental workflows. Production has one default evidence construction and scoring path, with a distinct Planner view because that consumer needs the full original evidence.

## Evidence

| Bank / Planner | R@10 | MRR@10 | R@100 | Lifecycle |
|---|---:|---:|---:|---:|
| V81 / frozen repeat 1 | 96.58% | 92.31% | 97.44% | 3/3 |
| V81 / frozen repeat 2 | 96.58% | 92.31% | 97.44% | 3/3 |
| V81 / live repeat 1 | 92.74% | 86.15% | 97.44% | 3/3 |
| V81 / live repeat 2 | 92.74% | 86.54% | 97.44% | 3/3 |
| V76 / live | 92.74% | 85.58% | 97.44% | 3/3 |

Live replay mean latency was 11.22, 11.41 and 11.24 seconds; P95 was 13.63, 13.26 and 13.01 seconds. All runs completed without request errors. These are Search replays on query-boundary snapshots, not fresh Add runs. Historical best R@10 was 88.03%, MRR 85.58%, and R@100 92.31%. Cross-bank live MRR ties the historical best, so stable strict improvement across all metrics remains unproven.

On the V81 fixed-plan comparison, all 16 candidate pools, RRF values and returned cards were identical before and after splitting the Jev view. The action condition improved from rank 4 to 1 and the actual contact from 2 to 1. Wrong-date workshop and device evidence remained at rank 2. V83's adjacent context increases average evidence volume; V84 does not further increase returned content.

Validation: 521 full regression tests passed, including the result-comparability audit tests. Seven focused provider tests passed after explicitly limiting the scoring-view override to `original` and `context`. Tests check actual Jev payloads, full external original preservation, source IDs, temporal operators, context-limit recovery and unchanged input cards. A full V84 fresh-bank run is required before completion; the frozen V83 run continues as a baseline.

## Third-bank live stability check

The completed V83 fresh-ingestion bank was replayed with V84 using query-boundary snapshots and live Planner/Jev calls. Repeat 1 reached R@10 96.5812%, MRR@10 92.3077%, R@100 97.4359%; repeat 2 reached R@10 88.8889%, MRR@10 84.6154%, R@100 97.4359%. Both passed lifecycle 3/3 without request errors. Search mean/P95 was 11.63/14.18 seconds and 11.50/15.15 seconds. Reports are `search-v84-live-v83-20261102-r1` and `r2`.

Repeat 2's action query produced navigation targets for reminder setup and motivational strategies under constraint/latest_state roles. Its two gold conditions fell from ranks 3/1 to 20/11. This demonstrates remaining Planner-to-ranker propagation of an incorrect retrieval objective. The stable-improvement criterion is still unmet despite the strong full V83 result. Production remains frozen while a generic objective-authority instruction is checked on independent synthetic examples.

## Completed V84 fresh-ingestion run

`golden-v84-primary-context-20261102-r1/golden-gpt-4o-mini` completed 47 Add calls, 13 main queries and all three lifecycle queries. R@10 is 92.7350%, MRR@10 92.3077%, R@100 97.4359%, and lifecycle passes 3/3. All requests succeeded. One invalid model response was repaired; no provider timeout was recorded. Add mean/P95 latency is 56.96/126.86 seconds, Search mean/P95 is 11.21/13.38 seconds. Average returned content is 179,977 bytes across 39.15 cards.

The fresh run exceeds all recorded historical-best metrics, while the third-bank live-repeat regression above remains evidence against a stable-completion claim. Current production stays V84 while independent Planner experiments investigate action-query drift.
