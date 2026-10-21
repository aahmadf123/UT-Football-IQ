import subprocess

from pipeline import producer_contract


def test_checkout_inventory_is_cached_at_first_advertisement(monkeypatch):
    calls = []

    def git(command, **kwargs):
        calls.append(command)
        return "a" * 40 + "\n" if command[-1] == "HEAD" else " M pipeline/module.py\n"

    producer_contract.source_revision.cache_clear()
    monkeypatch.setattr(producer_contract.subprocess, "check_output", git)
    try:
        assert producer_contract.source_revision() == {"git_sha": "a" * 40, "git_dirty": True}
        assert producer_contract.source_revision() == {"git_sha": "a" * 40, "git_dirty": True}
        assert len(calls) == 2
    finally:
        producer_contract.source_revision.cache_clear()


def test_missing_git_keeps_inventory_unknown_without_blocking_cpu_worker(monkeypatch):
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git")

    producer_contract.source_revision.cache_clear()
    monkeypatch.setattr(producer_contract.subprocess, "check_output", unavailable)
    try:
        assert producer_contract.source_revision() == {"git_sha": "unknown", "git_dirty": None}
    finally:
        producer_contract.source_revision.cache_clear()
