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
Search replays and complete Golden runs. A Search replay over a completed bank
can include later-ingested evidence and is compared only with equivalent replays.
