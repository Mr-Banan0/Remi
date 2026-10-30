# Remi

Remi is a source-grounded lifelong memory service for personal agents.

## Memory skeleton

```text
Conversation source
└── Subject (person | animal | organization)
    └── Memory (fact | preference | experience | event)
        ├── Revision (source-backed history)
        └── Edge (mentions | supported_by | revises | related)
```
