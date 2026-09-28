from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
import os
import subprocess

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

import database
import main
import models
import schemas


ROOT = Path(__file__).resolve().parents[1]


def _scope(prefix):
    return f"{prefix}_{uuid4().hex[:10]}"


def _tenant(session, tenant_id=None):
    tenant_id = tenant_id or _scope("phase34_tenant")
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    tenant = models.Tenant(id=tenant_id, name=tenant_id, slug=tenant_id)
    session.add(tenant)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return tenant


def _member(session, tenant_id, *, role="member", google=False):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    email = f"{uuid4().hex}@example.com"
    user = models.User(email=email, password_hash="test", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(
        tenant_id=tenant_id,
        user_id=user.id,
        name=email,
        email=email,
        role=role,
        google_refresh_token="refresh-token" if google else None,
    )
    session.add(member)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def _client(session, name="Client"):
    client = models.Client(name=name, code=_scope("C")[:24])
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def _task(session, member, client, name="Task"):
    task = models.TaskInstance(
        client_id=client.id,
        client_name=client.name,
        name=name,
        owner_id=member.id,
        status="todo",
        segments=[],
        role="",
        task_type="",
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def test_backup_script_fails_closed_without_database_url(tmp_path):
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / "backup_postgres.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "DATABASE_URL is required" in result.stderr


def test_backup_script_uses_custom_format_and_requested_destination(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "pg_dump_args.txt"
    fake = bindir / "pg_dump"
    fake.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > '{log}'\n", encoding="utf-8")
    fake.chmod(0o755)
    backup_dir = tmp_path / "backups"
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": "postgresql://clockbook:test@localhost/clockbook_test",
        "BACKUP_DIR": str(backup_dir),
        "PATH": f"{bindir}:{env.get('PATH', '')}",
    })
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / "backup_postgres.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    args = log.read_text(encoding="utf-8").splitlines()
    assert "--format=custom" in args
    assert "--no-owner" in args
    assert "--no-privileges" in args
    assert env["DATABASE_URL"] == args[-1]
    output_arg = next(arg for arg in args if arg.startswith("--file="))
    assert str(backup_dir) in output_arg
    assert "Backup created:" in result.stdout


def test_restore_script_refuses_production_like_target_before_pg_restore(tmp_path):
    backup = tmp_path / "backup.dump"
    backup.write_bytes(b"test")
    env = os.environ.copy()
    env["RESTORE_DATABASE_URL"] = "postgresql://user:pass@db/clockbook-production"
    result = subprocess.run(
        ["bash", str(ROOT / "ops" / "restore_postgres.sh"), str(backup)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "Refusing to restore" in result.stderr


def test_restore_script_uses_clean_safe_restore_flags_for_nonproduction(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "pg_restore_args.txt"
    fake = bindir / "pg_restore"
    fake.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > '{log}'\n", encoding="utf-8")
    fake.chmod(0o755)
    backup = tmp_path / "backup.dump"
    backup.write_bytes(b"test")
    env = os.environ.copy()
    env.update({
        "RESTORE_DATABASE_URL": "postgresql://user:pass@db/clockbook_staging",
        "PATH": f"{bindir}:{env.get('PATH', '')}",
    })
    subprocess.run(
        ["bash", str(ROOT / "ops" / "restore_postgres.sh"), str(backup)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    args = log.read_text(encoding="utf-8").splitlines()
    assert "--clean" in args
    assert "--if-exists" in args
    assert "--no-owner" in args
    assert "--no-privileges" in args
    assert f"--dbname={env['RESTORE_DATABASE_URL']}" in args
    assert args[-1] == str(backup)


def test_hardening_migrations_are_rerunnable_and_preserve_existing_data(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-idempotent.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE clients (id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, name VARCHAR NOT NULL, code VARCHAR)"
        ))
        conn.execute(text(
            "INSERT INTO clients (id, tenant_id, name, code) VALUES ('legacy', 'tenant_x', 'Legacy', 'LEG')"
        ))
        conn.execute(text(
            "CREATE TABLE audit_events (id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, created_at DATETIME, "
            "actor_member_id VARCHAR, action VARCHAR, entity_type VARCHAR, entity_id VARCHAR, changes JSON)"
        ))
    monkeypatch.setattr(main, "engine", engine)
    main.run_hardening_migrations()
    main.run_hardening_migrations()
    columns = {c["name"] for c in inspect(engine).get_columns("clients")}
    assert "version" in columns
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id, name, code, version FROM clients WHERE id='legacy'" )).first()
    assert row == ("legacy", "Legacy", "LEG", 1)


class _FakeGoogleResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.is_success:
            raise RuntimeError(f"http {self.status_code}")


def test_quick_meeting_retry_reuses_same_task_and_external_event(monkeypatch):
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member = _member(s, tenant.id, google=True)
        request_id = _scope("phase34_meeting")
        post_calls = []
        event = {
            "id": "calendar-event-1",
            "hangoutLink": "https://meet.example/test",
            "htmlLink": "https://calendar.example/event",
        }
        monkeypatch.setattr(main, "get_google_access_token", lambda current_member, db: "access-token")

        def fake_post(*args, **kwargs):
            post_calls.append((args, kwargs))
            return _FakeGoogleResponse(event)

        monkeypatch.setattr(main.httpx, "post", fake_post)
        monkeypatch.setattr(main.httpx, "get", lambda *args, **kwargs: _FakeGoogleResponse(event))
        payload = schemas.QuickMeetingCreate(summary="Retry-safe meeting", request_id=request_id, duration_minutes=30)
        first = main.create_quick_meeting(payload, current_member=member, db=s)
        second = main.create_quick_meeting(payload, current_member=member, db=s)
        assert first["task"]["id"] == second["task"]["id"]
        assert second["reused"] is True
        assert len(post_calls) == 1
        assert s.query(models.TaskInstance).filter(models.TaskInstance.quick_meeting_request_id == request_id).count() == 1
    finally:
        s.close()


def test_rapid_timer_start_pause_cycles_do_not_create_negative_or_open_segments():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member = _member(s, tenant.id)
        client = _client(s)
        task = _task(s, member, client, "Rapid timer")
        for _ in range(40):
            main.start_task(task.id, schemas.TaskStart(), current_member=member, db=s)
            main.pause_task(task.id, schemas.TaskPause(), current_member=member, db=s)
        s.refresh(task)
        assert task.status == "paused"
        assert len(task.segments) == 40
        assert all(seg.get("start") and seg.get("end") for seg in task.segments)
        assert main.elapsed_seconds(task.segments) >= 0
        for seg in task.segments:
            assert main.parse_utc_naive(seg["end"]) >= main.parse_utc_naive(seg["start"])
    finally:
        s.close()


def test_stale_admin_update_cannot_overwrite_a_newer_client_change():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        admin = _member(s, tenant.id, role="super_admin")
        client = _client(s, "Original")
        stale_version = client.version
        first = main.update_client(
            client.id,
            schemas.ClientCreate(name="Newer value", code=client.code, expected_version=stale_version),
            current_member=admin,
            db=s,
        )
        assert first.name == "Newer value"
        with pytest.raises(HTTPException) as exc:
            main.update_client(
                client.id,
                schemas.ClientCreate(name="Stale value", code=client.code, expected_version=stale_version),
                current_member=admin,
                db=s,
            )
        assert exc.value.status_code == 409
        s.refresh(client)
        assert client.name == "Newer value"
    finally:
        s.close()


def test_extreme_payloads_are_rejected_at_schema_boundary():
    with pytest.raises(ValidationError):
        schemas.ClientCreate(name="x" * 241, code="OK")
    with pytest.raises(ValidationError):
        schemas.TaskRecoverTime(seconds=28801)
    with pytest.raises(ValidationError):
        schemas.TaskSubmit(note="x" * 4001)
    with pytest.raises(ValidationError):
        schemas.ClientImportRequest(rows=[schemas.ClientImportRow(name=f"Client {i}", code=f"C{i}") for i in range(5001)])
    with pytest.raises(ValidationError):
        schemas.MemberCapacityUpdate(weekly_capacity_hours=169, expected_version=1)


def test_cross_tenant_client_id_cannot_be_injected_into_task_creation():
    s = database.SessionLocal()
    try:
        tenant_a = _tenant(s)
        member_a = _member(s, tenant_a.id)
        tenant_b = _tenant(s)
        client_b = _client(s, "Other tenant client")
        s.info["tenant_id"] = tenant_a.id
        with pytest.raises(HTTPException) as exc:
            main.create_task(
                schemas.TaskCreate(client_id=client_b.id, client_name=client_b.name, name="Injected task"),
                current_member=member_a,
                db=s,
            )
        assert exc.value.status_code == 404
    finally:
        s.close()


def test_large_export_is_not_silently_truncated():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        admin = _member(s, tenant.id, role="super_admin")
        client = _client(s, "Volume client")
        start = datetime(2026, 9, 1, 9, 0, 0)
        tasks = []
        for i in range(300):
            seg_start = start + timedelta(minutes=i)
            tasks.append(models.TaskInstance(
                client_id=client.id,
                client_name=client.name,
                name=f"Volume task {i}",
                owner_id=admin.id,
                status="submitted",
                submitted_by_id=admin.id,
                submitted_at=seg_start + timedelta(seconds=30),
                segments=[{"start": seg_start.isoformat() + "Z", "end": (seg_start + timedelta(seconds=30)).isoformat() + "Z"}],
                role="",
                task_type="",
            ))
        s.add_all(tasks)
        s.commit()
        rows = main.get_export(pushed="all", current_member=admin, db=s)
        volume_rows = [row for row in rows if row["client"] == client.name]
        assert len(volume_rows) == 300
        assert {row["task"] for row in volume_rows} == {f"Volume task {i}" for i in range(300)}
    finally:
        s.close()


def test_rate_limit_buckets_are_isolated_between_identities():
    s = database.SessionLocal()
    try:
        a = _scope("phase34_rate_a")
        b = _scope("phase34_rate_b")
        main._enforce_rate_limit(s, a, limit=1, window_seconds=900)
        with pytest.raises(HTTPException) as exc:
            main._enforce_rate_limit(s, a, limit=1, window_seconds=900)
        assert exc.value.status_code == 429
        # A noisy or abusive identity must not consume another identity's allowance.
        main._enforce_rate_limit(s, b, limit=1, window_seconds=900)
    finally:
        s.close()
