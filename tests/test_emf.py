from typing import Any

from voice_agent.emf import EmfReporter


def reporter() -> EmfReporter:
    return EmfReporter(
        namespace="CollectionsChallenge", service="voice-agent", clock=lambda: 1700000000.0
    )


def service_doc(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(d for d in docs if "Stage" not in d)


def test_flush_reports_active_calls_every_time_even_when_idle() -> None:
    docs = reporter().flush(active_calls=7, in_flight=2, dlq_pending=None)
    doc = service_doc(docs)
    meta = doc["_aws"]["CloudWatchMetrics"][0]
    assert meta["Namespace"] == "CollectionsChallenge"
    assert meta["Dimensions"] == [["ServiceName"]]
    assert doc["ServiceName"] == "voice-agent"
    assert doc["ActiveCalls"] == 7 and doc["InFlightTurns"] == 2
    assert doc["_aws"]["Timestamp"] == 1700000000000
    assert "DlqPending" not in doc


def test_turns_and_latency_are_reported_then_reset() -> None:
    r = reporter()
    r.turn("completed", 0.5)
    r.turn("degraded", 1.25)
    r.turn("unrecorded", None)
    doc = service_doc(r.flush(active_calls=0, in_flight=0, dlq_pending=3))
    assert doc["Turns"] == 3 and doc["TurnsCompleted"] == 1 and doc["TurnsDegraded"] == 2
    assert doc["TurnLatency"] == [500.0, 1250.0]
    assert doc["DlqPending"] == 3
    names = {m["Name"]: m.get("Unit") for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert names["TurnLatency"] == "Milliseconds"
    again = service_doc(r.flush(active_calls=0, in_flight=0, dlq_pending=None))
    assert again["Turns"] == 0 and "TurnLatency" not in again


def test_latency_values_are_split_into_documents_of_at_most_100() -> None:
    r = reporter()
    for _ in range(250):
        r.turn("completed", 0.1)
    docs = [d for d in r.flush(active_calls=0, in_flight=0, dlq_pending=None) if "TurnLatency" in d]
    assert [len(d["TurnLatency"]) for d in docs] == [100, 100, 50]
    assert sum(d.get("Turns", 0) for d in docs) == 250  # counts reported once


def test_stage_documents_carry_attempts_failures_retries_and_breaker_state() -> None:
    r = reporter()
    r.attempt("llm", "ok", retry=False)
    r.attempt("llm", "error", retry=True)
    r.attempt("llm", "rejected", retry=True)
    r.breaker("llm", 2)
    docs = [
        d for d in r.flush(active_calls=0, in_flight=0, dlq_pending=None) if d.get("Stage") == "llm"
    ]
    assert len(docs) == 1
    doc = docs[0]
    assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["ServiceName", "Stage"]]
    assert doc["ProviderAttempts"] == 3 and doc["ProviderFailures"] == 1 and doc["Retries"] == 1
    assert doc["BreakerState"] == 2
    # breaker state is a gauge: still reported on the next flush
    nxt = next(
        d for d in r.flush(active_calls=0, in_flight=0, dlq_pending=None) if d.get("Stage") == "llm"
    )
    assert nxt["BreakerState"] == 2 and nxt["ProviderAttempts"] == 0
