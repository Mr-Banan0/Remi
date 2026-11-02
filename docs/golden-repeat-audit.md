# Golden comparison audit

`scripts.audit_golden_runs` reads existing reports and writes a fresh JSON audit. It recomputes R@10, MRR@10 and R@100 from question evidence ranks, keeps incomplete-run metrics null, checks duplicate/missing questions, exposes lifecycle counts and provider errors, and distinguishes full execution, live Search replay and frozen-planner replay. It compares dataset hashes and common model, interface and resource settings, then verifies available runtime directories and dataset files against recorded fingerprints.

Algorithm-specific settings are reported separately. For example, historical V61 has update similarity and margin thresholds; those are part of the old method rather than the shared evaluation resource budget. The comparison does not silently treat them as equivalent algorithms.

Example:

```sh
PYTHONPATH=src:. .venv/bin/python -B -m scripts.audit_golden_runs \
  --baseline baseline-one/result.json baseline-two/result.json \
  --run complete-run/result.json replay/result.json running-run/result.json \
  --output /private/tmp/golden-audit-new.json
```

The tool reports evidence and deltas; it does not declare the optimization goal complete. Fresh-ingestion stability, replay scope, output volume and semantic failures still require review. Three targeted tests cover incomplete runs, duplicate questions, resource mismatches, visible method differences and lifecycle failure reporting.

Audit `/private/tmp/remi-golden-audit-v84-20261102-r1.json` verified the recorded dataset and runtime fingerprints for V81, the three recent live combination replays, and the ongoing V83/V84 full runs. Shared evaluation configuration matches V60/V61. The V83 full run recorded one `add_links` timeout followed by a successful second attempt; no Add request failed at that checkpoint. Both ongoing full runs remain incomplete and receive no final metric verdict.
