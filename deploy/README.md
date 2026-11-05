# Long-running service operations

The service keeps business memories and every verified backup. Daily backups run at 03:00 server local time (up to ten minutes jitter). Capacity checks run every fifteen minutes. A missing or older-than-36-hours backup, less than 1 GiB free space, or a WAL larger than 512 MiB fails the monitor service and appears in the systemd journal. External notification delivery remains an operator integration; systemd failure is the currently installed alert surface.

`GET /metrics` requires the existing API credential. It exposes cumulative request counts, failures, in-flight requests, work counters and stage durations. Latency percentiles cover the last 1,024 completed requests per operation; counters reset on process restart. `REMI_METRICS_LOG=1` additionally records content-free request measurements in the service journal. Stage timings overlap and must be interpreted individually. Returned-row counters measure materialized work, not physical SQLite page scans.

`REMI_SEARCH_READ_CONCURRENCY` limits simultaneous SQLite read snapshots (default 4, supported 1–32). Set 1 on the current 2 GiB server, matching its declared low concurrency. Waiting is included in request latency and measured separately. Provider concurrency and quality settings remain unchanged.

## Deployment and recovery

1. Stage a clean release containing source and deployment files. Preserve the existing environment file and pinned dependency environment. Set `PYTHONPATH` to the new release's `src` so the selected source is explicit.
2. Copy the live database using SQLite's backup API. Validate integrity and row counts. Apply projection migration to that disposable copy and validate again. Restore a second fresh copy from the backup and compare row counts to demonstrate recoverability.
3. Check for active requests, gracefully stop the service, and take the final pre-migration backup. Upgrade every writer before adding the projection column. Apply the projection and WAL migrations, preserving all original records.
4. Point the service to the staged release, start it, and verify local and public health plus authenticated metrics. Preserve the old release and pre-migration database together: old writers use the old table layout and cannot operate on the migrated database.
5. Install the timers, run one verified backup immediately, and run the monitor. Inspect timer scheduling and journal status.

For rollback, stop the service and preserve the failed database and its sidecars under a separate name. Restore the pre-migration backup to a fresh path and switch the old service to that database. Reconcile any writes received after cutover before resuming. The backup utility refuses to overwrite an existing destination.

The dependency environment may remain attached to the previous release when package requirements have not changed. Preserve that release while the environment is referenced. A dependency update should build a fresh environment and repeat regression checks.

## Validation boundaries

`scripts/benchmark_sustained_search.py` measures deterministic local retrieval under concurrent callers. It reports throughput, response equivalence, resident-memory high-water mark, bounded aggregate metrics, and per-request durations. It excludes external provider/network latency. Online Golden supplies end-to-end quality and latency evidence separately.

Exact vector scoring and lightweight history scans still grow with data size. ANN and automatic memory eviction remain conditional engineering choices: adoption requires a quality/scale experiment demonstrating an acceptable recall trade-off. No business memory or backup is automatically deleted by these operations.
