"""Token statistics: estimation, ~500ms cadence, provider usage correction."""

from __future__ import annotations

from src.llm.token_stats import (
    DEFAULT_EMIT_INTERVAL_SEC,
    FinalStats,
    TokenStatsTracker,
    estimate_tokens,
)


class FakeClock:
    """Deterministic monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ------------------------------------------------------------------ estimation


def test_estimate_tokens_grows_with_chars() -> None:
    counts = [estimate_tokens("字" * n) for n in (0, 5, 10, 20, 40)]
    assert counts[0] == 0
    assert all(later > earlier for earlier, later in zip(counts, counts[1:], strict=False))


def test_estimate_tokens_cjk_is_denser_than_latin() -> None:
    assert estimate_tokens("你好世界你好世界") > estimate_tokens("abcdefgh")  # 8 chars each
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # ~4 latin chars per token


# -------------------------------------------------------------------- cadence


def test_default_stats_cadence_is_500ms() -> None:
    assert DEFAULT_EMIT_INTERVAL_SEC == 0.5


def test_should_emit_respects_the_interval() -> None:
    clock = FakeClock()
    tracker = TokenStatsTracker(clock=clock)
    tracker.start()

    clock.advance(0.499)
    assert tracker.should_emit() is False
    clock.advance(0.001)  # exactly 500ms since start
    assert tracker.should_emit() is True
    # Immediately after an emit the cadence restarts.
    assert tracker.should_emit() is False
    clock.advance(0.5)
    assert tracker.should_emit() is True


def test_snapshot_reports_tokens_per_second() -> None:
    clock = FakeClock()
    tracker = TokenStatsTracker(clock=clock)
    tracker.start()
    tracker.add("字" * 16)  # 16 CJK chars -> 10 tokens
    clock.advance(2.0)

    snapshot = tracker.snapshot()
    assert snapshot.estimated_tokens == tracker.estimated_tokens == 10
    assert snapshot.elapsed_ms == 2000
    assert snapshot.tokens_per_second == 5.0


def test_add_implicitly_starts_the_tracker() -> None:
    tracker = TokenStatsTracker(clock=FakeClock())
    assert tracker.started is False
    tracker.add("hi")
    assert tracker.started is True
    assert tracker.text == "hi"
    assert tracker.add("") == tracker.estimated_tokens  # empty delta is a no-op


# --------------------------------------------------------- provider correction


def test_finalize_without_usage_keeps_the_estimate() -> None:
    clock = FakeClock()
    tracker = TokenStatsTracker(clock=clock)
    tracker.start()
    tracker.add("字" * 16)
    clock.advance(1.0)

    final = tracker.finalize(None)
    assert isinstance(final, FinalStats)
    assert final.from_provider_usage is False
    assert final.completion_tokens == final.estimated_tokens == 10
    assert final.elapsed_ms == 1000
    assert final.tokens_per_second == 10.0


def test_finalize_with_provider_usage_corrects_the_number() -> None:
    clock = FakeClock()
    tracker = TokenStatsTracker(clock=clock)
    tracker.start()
    tracker.add("字" * 16)  # estimate: 10
    clock.advance(1.0)

    final = tracker.finalize(42)
    assert final.from_provider_usage is True
    assert final.completion_tokens == 42  # provider wins
    assert final.estimated_tokens == 10  # estimate preserved for diagnostics
    assert final.tokens_per_second == 42.0

    # set_usage during streaming has the same effect on snapshots.
    other = TokenStatsTracker(clock=FakeClock())
    other.add("hello world")
    other.set_usage(99)
    assert other.completion_tokens == 99
    assert other.snapshot().estimated_tokens == 99
    other.set_usage(None)  # ignored
    other.set_usage(-1)  # ignored
    assert other.completion_tokens == 99
