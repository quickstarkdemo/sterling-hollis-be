from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app import worker


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@contextmanager
def _fake_session_factory():
    yield object()


def _result(job_id: str, status: str = "succeeded") -> SimpleNamespace:
    return SimpleNamespace(
        id=job_id,
        status=SimpleNamespace(value=status),
        run_id="run_test",
        attempted=0,
        generated=0,
        skipped=0,
        failed_count=0,
    )


def _configure_worker_intervals(monkeypatch, *, poll: str = "1", max_idle: str = "4", stale: str = "60") -> None:
    monkeypatch.setenv("INDEX_WORKER_POLL_SECONDS", poll)
    monkeypatch.setenv("INDEX_WORKER_MAX_IDLE_SECONDS", max_idle)
    monkeypatch.setenv("INDEX_WORKER_STALE_RECOVERY_SECONDS", stale)
    get_settings.cache_clear()


def test_worker_idle_backoff_doubles_until_cap(monkeypatch):
    _configure_worker_intervals(monkeypatch)
    monkeypatch.setattr(worker, "process_next_image_generation_job", lambda session_factory: None)
    monkeypatch.setattr(worker, "process_next_index_job", lambda session_factory: None)
    monkeypatch.setattr(worker, "recover_stale_image_generation_jobs", lambda db: 0)

    sleeps: list[float] = []

    worker.run_background_worker(
        session_factory=_fake_session_factory,
        sleep_fn=sleeps.append,
        monotonic_fn=lambda: 0.0,
        random_fn=lambda start, end: 0.0,
        stop_after_iterations=4,
    )

    assert sleeps == [1.0, 2.0, 4.0, 4.0]


def test_worker_idle_backoff_resets_after_processed_job(monkeypatch):
    _configure_worker_intervals(monkeypatch)
    image_results = iter([None, None, _result("imgjob_test"), None])
    monkeypatch.setattr(worker, "process_next_image_generation_job", lambda session_factory: next(image_results))
    monkeypatch.setattr(worker, "process_next_index_job", lambda session_factory: None)
    monkeypatch.setattr(worker, "recover_stale_image_generation_jobs", lambda db: 0)

    sleeps: list[float] = []

    worker.run_background_worker(
        session_factory=_fake_session_factory,
        sleep_fn=sleeps.append,
        monotonic_fn=lambda: 0.0,
        random_fn=lambda start, end: 0.0,
        stop_after_iterations=4,
    )

    assert sleeps == [1.0, 2.0, 1.0]


def test_worker_runs_stale_recovery_on_configured_cadence(monkeypatch):
    _configure_worker_intervals(monkeypatch, stale="60")
    monkeypatch.setattr(worker, "process_next_image_generation_job", lambda session_factory: None)
    monkeypatch.setattr(worker, "process_next_index_job", lambda session_factory: None)

    recovery_calls = 0

    def recover(_db):
        nonlocal recovery_calls
        recovery_calls += 1
        return 0

    times = iter([0.0, 10.0, 20.0, 61.0])
    monkeypatch.setattr(worker, "recover_stale_image_generation_jobs", recover)

    worker.run_background_worker(
        session_factory=_fake_session_factory,
        sleep_fn=lambda seconds: None,
        monotonic_fn=lambda: next(times),
        random_fn=lambda start, end: 0.0,
        stop_after_iterations=4,
    )

    assert recovery_calls == 2
