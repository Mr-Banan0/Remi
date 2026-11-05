"""Bounded, content-free operational metrics shared by request snapshots."""
from collections import deque
from threading import Lock
from time import time
import json
import logging
import math
import os


class RuntimeMetrics:
    def __init__(self):
        self.lock = Lock()
        self.started_at = time()
        self.rows = {name: {"started": 0, "completed": 0, "failed": 0, "inflight": 0,
                     "seconds": 0.0, "latencies": deque(maxlen=1024), "work": {}, "stages": {}}
                     for name in ("add", "search")}

    def begin(self, operation):
        with self.lock:
            row = self.rows[operation]
            row["started"] += 1
            row["inflight"] += 1

    def finish(self, metrics):
        operation = metrics["operation"]
        seconds = metrics["total_seconds"]
        with self.lock:
            row = self.rows[operation]
            row["inflight"] -= 1
            row["completed"] += 1
            row["failed"] += not metrics["success"]
            row["seconds"] += seconds
            row["latencies"].append(seconds)
            for key, value in metrics["work"].items():
                if isinstance(value, (int, float)) and math.isfinite(value):
                    row["work"][key] = row["work"].get(key, 0) + value
            for key, value in metrics["stages"].items():
                stage = row["stages"].setdefault(key, {"calls": 0, "seconds": 0.0})
                stage["calls"] += value["calls"]
                stage["seconds"] += value["seconds"]
        if os.getenv("REMI_METRICS_LOG") == "1":
            logging.getLogger("uvicorn.error").info("remi_performance %s", json.dumps(metrics))

    def snapshot(self):
        with self.lock:
            result = {"started_at": self.started_at, "uptime_seconds": time()-self.started_at, "operations": {}}
            for operation, row in self.rows.items():
                values = sorted(row["latencies"])
                result["operations"][operation] = {
                    **{k: v for k, v in row.items() if k not in {"latencies", "work", "stages"}},
                    "window_count": len(values),
                    "p50_seconds": values[(len(values)-1)//2] if values else None,
                    "p95_seconds": values[math.ceil(len(values)*.95)-1] if values else None,
                    "work": dict(row["work"]), "stages": {k: dict(v) for k, v in row["stages"].items()}}
            return result
