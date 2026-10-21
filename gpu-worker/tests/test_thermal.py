"""The worker pauses while the GPU is hot and stays responsive while it waits."""

import pytest

from pipeline import thermal
from pipeline.runtime import PipelineInterrupted


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _governor(readings: list[float | None]) -> tuple[thermal.ThermalGovernor, FakeClock, list[float | None]]:
    clock = FakeClock()
    seen: list[float | None] = []

    def read() -> float | None:
        value = readings.pop(0) if readings else 60.0
        seen.append(value)
        return value

    return thermal.ThermalGovernor(read=read, sleep=clock.sleep, clock=clock, hot_c=80, cool_c=70), clock, seen


def test_cool_gpu_never_waits() -> None:
    governor, clock, _ = _governor([65.0])
    governor.pace()
    assert clock.now == 0 and governor.cool_downs == 0


def test_hot_gpu_waits_until_it_cools() -> None:
    governor, clock, seen = _governor([84.0, 79.0, 74.0, 69.0])
    governor.pace()
    assert seen == [84.0, 79.0, 74.0, 69.0]
    assert clock.now == 3 * thermal.CHECK_INTERVAL_S and governor.cool_downs == 1


def test_reads_are_rate_limited() -> None:
    governor, clock, seen = _governor([60.0, 90.0])
    governor.pace()
    governor.pace()  # within the interval: no second read
    assert seen == [60.0]
    clock.now += thermal.CHECK_INTERVAL_S
    governor.pace()
    assert seen[:2] == [60.0, 90.0]


def test_cancel_still_works_while_cooling() -> None:
    governor, _, _ = _governor([90.0, 90.0, 90.0])

    def cancelled() -> None:
        raise PipelineInterrupted("cancel")

    with pytest.raises(PipelineInterrupted):
        governor.pace(cancelled)


def test_unreadable_temperature_never_blocks() -> None:
    governor, clock, _ = _governor([None])
    governor.pace()
    assert clock.now == 0


def test_cool_down_has_a_ceiling() -> None:
    governor, clock, _ = _governor([95.0] * 1000)
    governor.pace()
    assert clock.now <= thermal.MAX_COOL_DOWN_S + thermal.CHECK_INTERVAL_S


def test_thread_cap_leaves_half_the_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_CPU_THREADS", raising=False)
    monkeypatch.setattr(thermal.os, "cpu_count", lambda: 16)
    assert thermal.cpu_threads() == 8
    monkeypatch.setenv("WORKER_CPU_THREADS", "3")
    assert thermal.cpu_threads() == 3
