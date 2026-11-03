# Remi

Remi is a source-grounded lifelong memory service for personal agents.

## Memory skeleton

```text
Conversation source
└── Memory (fact | preference | experience | event)
    ├── Revision → previous revision (revises)
    ├── Source evidence (supported_by)
    └── Other memory (related retrieval hint)
```

The current pipeline maintains memories directly. Names, participants and their
context remain in source-backed content. Earlier subject records remain readable
in stored banks; new ingestion uses the subjectless pipeline.

## Add

Save source → extract candidates → keyword and semantic recall → choose add/update
→ validate and optionally repair once → persist revisions → judge proposed related
pairs → update the graph and search index.

The maintenance model compares each candidate's identity before returning its
decision. Updates continue the same occurrence, proposition or experience.
Related edges connect separate memories for retrieval and carry no asserted causal
meaning. A changed revision triggers review of its affected related edges.

## Search

Keyword and semantic recall → supplemental searches and bounded graph expansion
→ deduplicate logical memories and fuse ranks → Jev reranking → source-backed
evidence packages. The AML adapter supports `top_k=100`.

Calendar queries preserve their supported precision. A month/day without a year
can retrieve matching days across visible years; relative dates use the request
reference time. Returned evidence retains dates and attribution.

The runtime version is defined in `src/remi/version.py`. Versioned experiment
reports under `docs/` distinguish offline tests, small provider checks, fixed-bank
Search replays and complete Golden runs. Query-boundary snapshot replays preserve
the evidence available at each query. Replays over a complete final bank can include
later-ingested evidence and are reported separately.

## Local setup

Use Python 3.12 or newer, starting in the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

Fill in the LLM, embedding and reranker credentials in the ignored `.env` file.
Review the provider base URLs for your own account. Configuration is loaded through
an explicit `--env-file` argument or `REMI_ENV_FILE`; existing process environment
variables take precedence. `check-config` reports credential presence without
printing key values:

```sh
.venv/bin/remi --env-file .env check-config
```

After approving creation of the six tables (`sources`, `memories`, `objects`,
`edges`, `requests`, `search_documents`), initialize a new local database explicitly:

```sh
.venv/bin/remi --env-file .env init-db --path .local-data/remi.sqlite3 --allow-create-schema
.venv/bin/remi --env-file .env serve --host 127.0.0.1 --port 8011
```

Set `REMI_DB_PATH` to the initialized path. `/health` reports process status;
`/ready` checks service configuration and storage initialization. Set
`MEMORY_API_KEY` when exposing the service and supply it through `Authorization:
Bearer <key>` or `X-API-Key`. Interactive API documentation is served at `/docs`.

## API contract

- `POST /add`: `request_id`, `user_id`, `session_id`, and `messages`. Each message
  has a `role`, `content`, and optional Unix-millisecond `timestamp`. Retry a paused
  request with the same ID and payload so its checkpoint can resume.
- `POST /search`: `user_id`, `query`, required `top_k` (1–100), and optional
  `options`. Returns `data` containing source-backed `id`, `content`, `score`, and
  `created_at` fields.

The complete request and response definitions are in
[`src/remi/models.py`](src/remi/models.py); HTTP behavior is in
[`src/remi/api.py`](src/remi/api.py).

## Code map

| Module | Responsibility |
| --- | --- |
| `add_pipeline.py` | Source ingestion, checkpoints and atomic Add orchestration |
| `memory_recall.py` | Candidate-specific keyword and semantic recall |
| `memory_maintenance.py` | Add/update validation and revision materialization |
| `memory_linking.py` | Proposed retrieval links and their validation |
| `engine.py`, `retrieval.py`, `graph.py` | Search orchestration, ranking and graph traversal |
| `source_context.py` | Bounded context for interpreting original evidence |
| `providers.py` | LLM, embedding and Jev adapters, schemas and prompts |
| `storage.py`, `revision_replay.py` | Persistence and historical view reconstruction |
| `settings.py`, `version.py` | Runtime configuration and public version identity |

The `subject_*` modules include existing-bank identity support and historical
validation helpers. The current Add path supplies an empty identity decision and
performs no subject-maintenance model call. Versioned reports describe the
architecture used by their own experiment, rather than the current runtime.

## Validation

After approving temporary test databases with the existing six-table schema, run:

```sh
REMI_TEST_SCHEMA_APPROVED=1 PYTHONPATH=src:. .venv/bin/python -B -m pytest -q -p no:cacheprovider
```

The test suite covers mocked providers and local invariants. Live provider checks
and Golden experiments are separate, credentialed runs. See
[`docs/golden-optimization-v85.md`](docs/golden-optimization-v85.md) for the V85
acceptance results, configuration, and remaining retrieval gaps. Those results
measure local Golden retrieval; they do not establish public leaderboard SOTA or
long-duration production performance.

## Incremental Search index and bounded cache

An explicitly migrated SQLite bank uses a persistent lexical index and loads full
candidate documents on demand. The index preserves Remi's tokenization and BM25
scores within each visible candidate pool. Vector ranking remains exact.

After approving the FTS index tables and SQLite-managed shadow tables, build the
index on a test copy first:

```sh
.venv/bin/remi index-db --path /path/to/test-copy.sqlite3 --allow-create-schema
```

Document writes maintain the index transactionally. A bounded process-local record
cache invalidates on writes, rollback, and external commits. Existing banks keep
the previous read path during the explicit migration window. See
[`docs/search-index-performance.md`](docs/search-index-performance.md) for migration,
validation scope, remaining scale limits, and the decision to keep Redis optional.
