"""Tests for the reports router (Issue #111).

Exercises the create / list / get / download endpoints plus the rendering
helpers in :mod:`app.reports`.  Storage and the background task are stubbed
so tests are hermetic and do not require object-store credentials.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from app.database import get_db
from app.deps import get_current_user, require_coach_or_above
from app.main import app
from app.models import JobStatus, ReportFormat, ReportJob, User, UserRole
from fastapi.testclient import TestClient


def _make_user(role: UserRole = UserRole.coach, user_id: uuid.UUID | None = None) -> User:
    u = MagicMock(spec=User)
    u.id = user_id or uuid.uuid4()
    u.role = role
    u.is_active = True
    return u


def _make_job(
    *,
    job_id: uuid.UUID | None = None,
    requested_by: uuid.UUID | None = None,
    status: JobStatus = JobStatus.queued,
    fmt: ReportFormat = ReportFormat.pdf,
    output_uri: str | None = None,
) -> ReportJob:
    j = MagicMock(spec=ReportJob)
    j.id = job_id or uuid.uuid4()
    j.report_type = "coaching_summary"
    j.format = fmt
    j.status = status
    j.parameters = {"sections": []}
    j.output_uri = output_uri
    j.error_message = None
    j.requested_by = requested_by or uuid.uuid4()
    j.started_at = None
    j.finished_at = None
    j.created_at = datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
    return j


class _CapturingDB:
    def __init__(
        self,
        *,
        list_rows: list[ReportJob] | None = None,
        detail_row: ReportJob | None = None,
    ) -> None:
        self.list_rows = list_rows or []
        self.detail_row = detail_row
        self.added: list[Any] = []
        self.last_sql: str = ""

    async def execute(self, stmt: Any) -> Any:
        self.last_sql = str(stmt)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.detail_row
        scalars = MagicMock()
        scalars.all.return_value = self.list_rows
        result.scalars.return_value = scalars
        return result

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _install_db(fake: _CapturingDB) -> None:
    async def _gen() -> AsyncGenerator[_CapturingDB, None]:
        yield fake

    app.dependency_overrides[get_db] = _gen


@pytest.fixture(autouse=True)
def _clear_overrides() -> Any:
    yield
    app.dependency_overrides.clear()


# ── POST /api/v1/reports ──────────────────────────────────────────────────────


def test_create_report_returns_queued_job(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _CapturingDB()
    _install_db(fake)
    coach = _make_user(UserRole.coach)
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[require_coach_or_above] = lambda: coach

    # Prevent the actual background task from running (it would hit the object store).
    monkeypatch.setattr(
        "app.routers.reports._run_report_job",
        lambda *args, **kwargs: None,
    )

    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/reports",
            json={"report_type": "coaching_summary", "format": "pdf"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["format"] == "pdf"
    assert body["report_type"] == "coaching_summary"
    assert uuid.UUID(body["id"])
    assert body["parameters"]["sections"]  # populated with all sections
    assert len(fake.added) == 1
    assert isinstance(fake.added[0], ReportJob)


def test_create_report_rejects_unknown_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _CapturingDB()
    _install_db(fake)
    coach = _make_user(UserRole.coach)
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[require_coach_or_above] = lambda: coach
    monkeypatch.setattr("app.routers.reports._run_report_job", lambda *a, **kw: None)

    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/reports",
            json={"report_type": "evil_report", "format": "pdf"},
        )
    assert resp.status_code == 400


def test_create_report_rejects_unknown_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _CapturingDB()
    _install_db(fake)
    coach = _make_user(UserRole.coach)
    app.dependency_overrides[get_current_user] = lambda: coach
    app.dependency_overrides[require_coach_or_above] = lambda: coach
    monkeypatch.setattr("app.routers.reports._run_report_job", lambda *a, **kw: None)

    with TestClient(app) as c:
        resp = c.post(
            "/api/v1/reports",
            json={"format": "json", "sections": ["bogus section"]},
        )
    assert resp.status_code == 400


def test_create_report_forbidden_for_viewer() -> None:
    fake = _CapturingDB()
    _install_db(fake)
    viewer = _make_user(UserRole.viewer)
    app.dependency_overrides[get_current_user] = lambda: viewer
    # Do not override require_coach_or_above — the real guard rejects.

    with TestClient(app) as c:
        resp = c.post("/api/v1/reports", json={"format": "pdf"})
    assert resp.status_code == 403


# ── GET /api/v1/reports ───────────────────────────────────────────────────────


def test_list_reports_scopes_to_requesting_user_for_coach() -> None:
    coach = _make_user(UserRole.coach)
    my_job = _make_job(requested_by=coach.id)
    fake = _CapturingDB(list_rows=[my_job])
    _install_db(fake)
    app.dependency_overrides[get_current_user] = lambda: coach

    with TestClient(app) as c:
        resp = c.get("/api/v1/reports")
    assert resp.status_code == 200, resp.text
    # A WHERE clause on requested_by must reach SQL — distinguish from the
    # column appearing in the SELECT projection.
    sql = fake.last_sql.lower()
    assert "where report_jobs.requested_by" in sql or "where requested_by" in sql


def test_list_reports_unscoped_for_admin() -> None:
    admin = _make_user(UserRole.admin)
    fake = _CapturingDB(list_rows=[])
    _install_db(fake)
    app.dependency_overrides[get_current_user] = lambda: admin

    with TestClient(app) as c:
        resp = c.get("/api/v1/reports")
    assert resp.status_code == 200
    # Admins must not have a WHERE clause on requested_by.
    sql = fake.last_sql.lower()
    assert "where" not in sql


# ── GET /api/v1/reports/{id} ──────────────────────────────────────────────────


def test_get_report_forbids_other_users_report() -> None:
    coach = _make_user(UserRole.coach)
    other_owner = uuid.uuid4()
    job = _make_job(requested_by=other_owner)
    fake = _CapturingDB(detail_row=job)
    _install_db(fake)
    app.dependency_overrides[get_current_user] = lambda: coach

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/reports/{job.id}")
    assert resp.status_code == 403


def test_get_report_404_when_missing() -> None:
    coach = _make_user(UserRole.coach)
    fake = _CapturingDB(detail_row=None)
    _install_db(fake)
    app.dependency_overrides[get_current_user] = lambda: coach

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/reports/{uuid.uuid4()}")
    assert resp.status_code == 404


# ── GET /api/v1/reports/{id}/download ─────────────────────────────────────────


def test_download_409_when_not_ready() -> None:
    coach = _make_user(UserRole.coach)
    job = _make_job(requested_by=coach.id, status=JobStatus.running, output_uri=None)
    fake = _CapturingDB(detail_row=job)
    _install_db(fake)
    app.dependency_overrides[get_current_user] = lambda: coach

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/reports/{job.id}/download")
    assert resp.status_code == 409


def test_download_returns_presigned_url(monkeypatch: pytest.MonkeyPatch) -> None:
    coach = _make_user(UserRole.coach)
    job = _make_job(
        requested_by=coach.id,
        status=JobStatus.succeeded,
        output_uri="s3://artifacts/reports/abc.pdf",
    )
    fake = _CapturingDB(detail_row=job)
    _install_db(fake)
    app.dependency_overrides[get_current_user] = lambda: coach

    monkeypatch.setattr(
        "app.routers.reports.generate_download_url_for_uri",
        lambda uri, ttl=None: f"https://example.invalid/{uri}",
    )

    with TestClient(app) as c:
        resp = c.get(f"/api/v1/reports/{job.id}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["download_url"].startswith("https://example.invalid/")
    assert "expires_at" in body


# ── Rendering ─────────────────────────────────────────────────────────────────


def test_render_json_and_csv_produce_bytes() -> None:
    from app.reports import (
        CoachingSummaryData,
        Section,
        SectionRow,
        render_csv,
        render_json,
    )

    data = CoachingSummaryData(
        generated_at="2026-05-28T00:00:00Z",
        sections=[Section(title="X", rows=[SectionRow(label="a", value="1")])],
        totals={"clips": 0},
    )
    j = render_json(data)
    c = render_csv(data)
    assert isinstance(j, bytes) and b"sections" in j
    assert isinstance(c, bytes) and b"Section,Label,Value" in c


def test_render_pdf_produces_bytes() -> None:
    from app.reports import (
        CoachingSummaryData,
        Section,
        SectionRow,
        render_pdf,
    )

    data = CoachingSummaryData(
        generated_at="2026-05-28T00:00:00Z",
        sections=[Section(title="X", rows=[SectionRow(label="a", value="1")])],
        totals={"clips": 0},
    )
    pdf = render_pdf(data)
    assert pdf.startswith(b"%PDF-")
