"""L5: the cost ledger is indexed by principal, prunes on write (cost entries older than the
retention window, failed-turn marks older than the failure window) and answers ``usage_for`` from
that principal's own entries only. In-memory; the shared store is separate work."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.costs import CostLedger
from app.gateway.types import Usage

T0 = datetime(2026, 10, 15, 12, tzinfo=UTC)


def make():
    now = [T0]
    return CostLedger(lambda: now[0]), now


def test_old_entries_are_pruned_on_write_and_memory_stays_bounded() -> None:
    ledger, now = make()
    for minute in range(5_000):  # ~3.5 days of one entry a minute for two principals
        now[0] = T0 + timedelta(minutes=minute)
        for uid in ("a", "b"):
            ledger.record_gateway(uid, f"r{minute}", "FRAME", Usage(input_tokens=1, output_tokens=1), 1)
    assert len(ledger.entries()) <= 2 * (24 * 60 + 2), len(ledger.entries())


def test_pruning_does_not_change_the_rolling_daily_answer() -> None:
    ledger, now = make()
    for minute in range(3_000):
        now[0] = T0 + timedelta(minutes=minute)
        ledger.record_gateway("a", "r", "FRAME", Usage(input_tokens=2, output_tokens=1), 1)
    day = ledger.usage_for("a", window=timedelta(hours=24))
    assert day == Usage(input_tokens=2 * 1_440, output_tokens=1_440)


def test_failed_turn_marks_are_pruned_after_the_failure_window() -> None:
    ledger, now = make()
    for minute in range(300):
        now[0] = T0 + timedelta(minutes=minute)
        ledger.record_failed_paid_turn("a", "r")
    retained = ledger.failed_paid_turns("a", window=timedelta(days=30))
    assert retained <= 62, retained
    assert ledger.failed_paid_turns("a", window=timedelta(hours=1)) == 60


def test_an_idle_principal_is_dropped_when_others_write() -> None:
    ledger, now = make()
    ledger.record_gateway("idle", "r", "FRAME", Usage(input_tokens=5, output_tokens=5), 1)
    now[0] = T0 + timedelta(days=3)
    for i in range(300):  # a sweep runs every so many writes
        ledger.record_gateway("busy", f"r{i}", "FRAME", Usage(input_tokens=1, output_tokens=1), 1)
    assert [e.principal_id for e in ledger.entries()] == ["busy"] * 300


def test_usage_for_reads_only_the_principals_own_entries() -> None:
    ledger, _ = make()
    for i in range(2_000):
        ledger.record_gateway("noisy", f"r{i}", "FRAME", Usage(input_tokens=1, output_tokens=1), 1)
    ledger.record_gateway("quiet", "r", "FRAME", Usage(input_tokens=7, output_tokens=3), 1)

    class Trap(list):
        def __iter__(self):
            raise AssertionError("usage_for walked another principal's entries")

    internals = vars(ledger)
    index = next((v for v in internals.values() if isinstance(v, dict) and "noisy" in v), None)
    assert index is not None, "the ledger keeps no per-principal index"
    index["noisy"] = Trap()
    assert ledger.usage_for("quiet", window=timedelta(hours=24)) == Usage(input_tokens=7, output_tokens=3)
