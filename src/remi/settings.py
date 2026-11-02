from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_environment(path=None):
    """Read explicit private configuration without shell expansion or logging values."""
    filename = path or os.getenv("REMI_ENV_FILE")
    if not filename:
        return
    for raw in Path(filename).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        name = name.strip()
        if not name.replace("_", "").isalnum():
            raise ValueError("environment_name_invalid")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name, value)


def private_key(name, file_name=None):
    value = os.getenv(name)
    if value:
        return value.strip()
    filename = os.getenv(file_name) if file_name else None
    return Path(filename).read_text(encoding="utf-8").strip() if filename else None


@dataclass(frozen=True)
class Settings:
    hot_limit: int = 24
    candidate_limit: int = 40
    graph_limit: int = 12
    decision_bytes: int = 24000
    evidence_bytes: int = 6000
    context_bytes: int = 4000
    role_recall_min_score: float = 0.02
    add_timeout: float = 180
    search_timeout: float = 45
    graph_enabled: bool = True
    hot_enabled: bool = True

    def __post_init__(self):
        if any(not 1 <= value <= 200 for value in (self.hot_limit, self.candidate_limit, self.graph_limit)):
            raise ValueError("candidate_limits_invalid")
        if not 4000 <= self.decision_bytes <= 64000 or not 512 <= self.context_bytes <= self.decision_bytes // 2:
            raise ValueError("context_budget_invalid")
        if not 512 <= self.evidence_bytes <= 16000 or not 0 < self.add_timeout <= 1770 or not 0 < self.search_timeout <= 180:
            raise ValueError("call_budget_invalid")
        if not 0 <= self.role_recall_min_score <= 1:
            raise ValueError("role_recall_threshold_invalid")

    @classmethod
    def from_environment(cls):
        mapping = {"hot_limit": "HOT_LIMIT", "candidate_limit": "CANDIDATE_LIMIT", "graph_limit": "GRAPH_LIMIT",
                   "decision_bytes": "DECISION_BYTES", "evidence_bytes": "EVIDENCE_BYTES", "context_bytes": "CONTEXT_BYTES"}
        values = {field: int(os.getenv("REMI_" + env, getattr(cls(), field))) for field, env in mapping.items()}
        values.update(add_timeout=float(os.getenv("REMI_ADD_TIMEOUT", "180")),
                      search_timeout=float(os.getenv("REMI_SEARCH_TIMEOUT", "45")),
                      role_recall_min_score=float(os.getenv("REMI_ROLE_RECALL_MIN_SCORE", "0.02")),
                      graph_enabled=os.getenv("REMI_GRAPH_ENABLED", "1") == "1",
                      hot_enabled=os.getenv("REMI_HOT_ENABLED", "1") == "1")
        return cls(**values)
