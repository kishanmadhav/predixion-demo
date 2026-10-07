"""CloudWatch Embedded Metric Format: metrics written as structured log lines.

The awslogs driver ships stdout to CloudWatch Logs, which extracts every line carrying
an `_aws` block into metrics, so no PutMetricData permission is needed. Latencies are
sent as raw values, so CloudWatch computes true percentiles (p50/p90/p99) across tasks.

ActiveCalls is emitted on every flush with only the ServiceName dimension: averaged
over all tasks it is "calls per task", which is what target tracking scales on.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

MAX_VALUES = 100  # EMF limit on values per metric per document
STAGES = ("stt", "llm", "tts")


def write_stdout(doc: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(doc, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class EmfReporter:
    def __init__(
        self, *, namespace: str, service: str, clock: Callable[[], float] = time.time
    ) -> None:
        self._namespace = namespace
        self._service = service
        self._clock = clock
        self._turns: Counter[str] = Counter()
        self._latencies_ms: list[float] = []
        self._dead_letters = 0
        self._replays: Counter[str] = Counter()
        self._attempts: dict[str, Counter[str]] = {s: Counter() for s in STAGES}
        self._breaker: dict[str, int] = dict.fromkeys(STAGES, 0)

    def turn(self, status: str, seconds: float | None) -> None:
        self._turns[status] += 1
        if seconds is not None:
            self._latencies_ms.append(round(seconds * 1000, 1))

    def attempt(self, stage: str, outcome: str, *, retry: bool) -> None:
        counts = self._attempts.setdefault(stage, Counter())
        counts["attempts"] += 1
        if outcome == "error":
            counts["failures"] += 1
        if retry and outcome != "rejected":
            counts["retries"] += 1

    def breaker(self, stage: str, value: int) -> None:
        self._breaker[stage] = value

    def dead_letter(self) -> None:
        self._dead_letters += 1

    def replay(self, status: str) -> None:
        self._replays[status] += 1

    def _doc(
        self, dimensions: list[str], values: dict[str, Any], units: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "_aws": {
                "Timestamp": int(self._clock() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": [dimensions],
                        "Metrics": [
                            {"Name": name, "Unit": units.get(name, "Count")} for name in values
                        ],
                    }
                ],
            },
            "ServiceName": self._service,
            **values,
        }

    def flush(
        self, *, active_calls: int, in_flight: int, dlq_pending: int | None
    ) -> list[dict[str, Any]]:
        turns = sum(self._turns.values())
        service: dict[str, Any] = {
            "ActiveCalls": active_calls,
            "InFlightTurns": in_flight,
            "Turns": turns,
            "TurnsCompleted": self._turns["completed"],
            "TurnsDegraded": turns - self._turns["completed"],
            "DeadLetters": self._dead_letters,
            "Replays": sum(self._replays.values()),
            "ReplaysResolved": self._replays["resolved"],
        }
        if dlq_pending is not None:
            service["DlqPending"] = dlq_pending
        chunks = [
            self._latencies_ms[i : i + MAX_VALUES]
            for i in range(0, len(self._latencies_ms), MAX_VALUES)
        ]
        units = {"TurnLatency": "Milliseconds"}
        docs = []
        if chunks:
            service["TurnLatency"] = chunks[0]
        docs.append(self._doc(["ServiceName"], service, units))
        docs += [self._doc(["ServiceName"], {"TurnLatency": c}, units) for c in chunks[1:]]
        for stage, counts in self._attempts.items():
            stage_doc = self._doc(
                ["ServiceName", "Stage"],
                {
                    "ProviderAttempts": counts["attempts"],
                    "ProviderFailures": counts["failures"],
                    "Retries": counts["retries"],
                    "BreakerState": self._breaker.get(stage, 0),
                },
                {},
            )
            stage_doc["Stage"] = stage
            docs.append(stage_doc)
        self._turns.clear()
        self._latencies_ms.clear()
        self._dead_letters = 0
        self._replays.clear()
        self._attempts = {s: Counter() for s in self._attempts}
        return docs
