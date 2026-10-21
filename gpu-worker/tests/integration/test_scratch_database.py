"""The seeding guard accepts only named loopback scratch databases."""

import pytest
from scratch_database import require_scratch_database


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://fixture:unused@127.0.0.1:55432/footiq_overhaul_completion",
        "postgresql+asyncpg://fixture:unused@localhost:5432/footiq_test",
        "postgresql+asyncpg://fixture:unused@[::1]:15432/footiq_test",
        "postgresql+asyncpg://fixture@localhost/footiq_test",
    ],
)
def test_actual_local_and_ci_scratch_destinations_are_accepted(url):
    require_scratch_database(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://fixture@db.example.invalid:5432/footiq_test",
        "postgresql+asyncpg://fixture@192.0.2.1:5432/footiq_overhaul_completion",
        "postgresql+asyncpg://fixture@localhost:5432/footiq",
        "postgresql+asyncpg://fixture@127.0.0.1:55432/production",
        "postgresql+asyncpg://fixture@localhost.evil.invalid/footiq_test",
        "postgresql+asyncpg://fixture@localhost/footiq_test?host=db.example.invalid",
        "postgresql+asyncpg://fixture@localhost/footiq_test?hostaddr=192.0.2.1",
        "postgresql+asyncpg://fixture@localhost/footiq_test?service=production",
        "postgresql+asyncpg://fixture@localhost,db.example.invalid/footiq_test",
        "postgresql+asyncpg:///footiq_test",
        "postgresql+asyncpg://fixture@localhost/footiq_test#production",
        "postgresql+asyncpg://fixture@localhost/footiq_test/production",
        "postgresql+asyncpg://fixture@localhost:invalid/footiq_test",
        "postgresql+asyncpg://fixture@localhost:0/footiq_test",
        "mysql://fixture@localhost/footiq_test",
        "",
    ],
)
def test_remote_production_and_overridden_destinations_are_refused(url):
    with pytest.raises(ValueError, match="loopback scratch database") as failure:
        require_scratch_database(url)
    assert url not in str(failure.value) if url else True
