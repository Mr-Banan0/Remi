# AML Academic submission — Remi 0.2.0

## Submission identity

- System name: Remi
- Version: `remi-0.2.0`
- Deployment version: `remi-0.2.0-cn-20261030`
- Evaluation type: Textual Memory
- Participant division: Academic / Open-source
- Organization / team: individual participant; no team
- Contact name and email: supplied privately in the AML access request
- Public display name: Remi
- Original author: Ye Sam (`Mr-Banan0`)
- Paper: none; technical design is documented in `docs/current-architecture.md`
- License: Apache License 2.0

## Academic model configuration

- LLM-related Add and Search components: `gpt-4o-mini`
- Embeddings: `text-embedding-v4`, 1,024 dimensions
- Search reranker: `jev-1.13.0`

This matches the Cycle 2 Academic rules: `text-embedding-v4` for embeddings,
`gpt-4o-mini` for LLM-related components, and unrestricted rerankers.

## Product description

Remi is a long-term memory service for personal agents. It stores the original
conversation evidence and maintains source-backed facts, preferences,
experiences, events, subject identities, revisions, and graph relations. Search
returns ranked memory evidence and original source excerpts for the platform's
answer and evaluation stages.

Add persists each accepted request synchronously. It identifies subjects,
extracts supported memory candidates, maintains revisions, writes the six-table
SQLite store, and updates the retrieval documents before returning HTTP 200.
Search performs lexical and vector recall, query planning, temporal revision
resolution, bounded graph expansion, reciprocal-rank fusion, and reranking.

## Hosted API

- Base URL: `https://api.bananagreatagain.com`
- Health: `GET /health`
- Add: `POST /add`
- Search: `POST /search`
- Authentication: `Authorization: Token <Memory System Key>`
- Health authentication: none
- Credential delivery: enter the dedicated Memory System Key only in the AML
  secure credential field. Keep it out of URLs, screenshots, email bodies,
  repositories, and chat messages.

## Contract behavior

- Add is synchronous and returns HTTP 200 after persistence.
- Add idempotency is scoped by `user_id` and `request_id`.
- Repeating the same request returns success; reusing a request ID with a
  different payload returns HTTP 409.
- Search isolation uses the exact `user_id` supplied by Add.
- Search accepts `top_k` from 1 to 100 and returns at most that number of items.
- Search returns ranked memory evidence and source excerpts, leaving final
  answering to AML.
- Health is an unauthenticated GET and returns HTTP 2xx while the service is
  available.

## Runtime and capacity declaration

- Region: Tencent Cloud Guangzhou, mainland China
- Compute: 2 vCPU, 2 GB RAM
- Public bandwidth: 4 Mbps, 300 GB monthly transfer
- Storage: 50 GB system disk with a persistent SQLite database
- Process model: one Remi API worker managed by systemd
- Reverse proxy: Nginx with TLS termination
- Declared maximum Add concurrency: 1
- Declared Search concurrency: 1
- Add timeout: 900 seconds
- Search timeout: 120 seconds
- Platform configuration: set Add and Search concurrency to the declared values
- Rate-limit behavior: requests within the declared concurrency are accepted;
  the platform should keep concurrency at the declared values
- Availability: systemd starts the service at boot and restarts it after a
  process failure

## Data and security

- Database: `/opt/remi-test/data/remi.sqlite3`
- Approved tables: `sources`, `memories`, `objects`, `edges`, `requests`, and
  `search_documents`
- API process binding: `127.0.0.1:8011`
- Public ingress: HTTPS through Nginx only
- Provider credentials and the Memory System Key are stored in a mode-600
  server configuration file readable by the service account
- Evaluation samples remain isolated by `user_id`

## Verification evidence

- Internal health and readiness checks passed on 2026-10-30.
- Public HTTPS health returned HTTP 200 in 0.063 seconds.
- An unauthenticated Add request returned HTTP 401.
- An authenticated synchronous Add returned HTTP 200 in 10.47 seconds and
  persisted a source-backed preference memory.
- Repeating the same Add request returned HTTP 200 idempotently in 0.071 seconds.
- An authenticated Search returned HTTP 200 in 18.54 seconds and retrieved the
  inserted evidence.
- Ten consecutive public health requests all returned HTTP 200.
- The service remained active with approximately 1.25 GB memory and 44 GB disk
  available after validation.

## Operational notes

- The submitted version is frozen as `remi-0.2.0-cn-20261030` for the AML
  access request and subsequent Smoke evaluation.
- The service uses a dedicated AML credential already generated on the server.
- Operational logs are available through `journalctl -u remi-test.service`.
- The SQLite database and configuration live outside the release directory, so
  service restarts preserve evaluation state.

## Repository disclosure

- Repository: `https://github.com/Mr-Banan0/Remi`
- License: Apache License 2.0
- Confirm public visibility and record the fixed evaluation commit before
  submitting the AML access request.
