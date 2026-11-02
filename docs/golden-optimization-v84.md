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

Validation: 518 full regression tests passed. Seven focused provider tests passed after explicitly limiting the scoring-view override to `original` and `context`. Tests check actual Jev payloads, full external original preservation, source IDs, temporal operators, context-limit recovery and unchanged input cards. A full V84 fresh-bank run is required before completion; the frozen V83 run continues as a baseline.
