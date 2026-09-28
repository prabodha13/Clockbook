from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
import importlib.util
import json
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database
import main
import models
import schemas


def _scope(prefix):
    return f"{prefix}_{uuid4().hex[:10]}"


def _tenant(session, tenant_id=None):
    tenant_id = tenant_id or _scope("phase2_tenant")
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


def _member(session, tenant_id, email=None, role="member"):
    email = email or f"{uuid4().hex}@example.com"
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="test-hash", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(
        tenant_id=tenant_id,
        user_id=user.id,
        name=email,
        email=email,
        role=role,
        password_hash="legacy-secret-hash",
        google_refresh_token="refresh-secret-value",
    )
    session.add(member)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member, user


def test_revoked_or_unknown_session_cannot_authenticate():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member, user = _member(s, tenant.id)
        token = _scope("phase2_session")
        s.add(models.Session(token=token, tenant_id=tenant.id, member_id=member.id, user_id=user.id))
        s.commit()
        assert main.get_current_member(f"Bearer {token}", s).id == member.id
        s.delete(s.get(models.Session, token))
        s.commit()
        with pytest.raises(HTTPException) as exc:
            main.get_current_member(f"Bearer {token}", s)
        assert exc.value.status_code == 401
    finally:
        s.close()


def test_session_cannot_be_rebound_to_a_different_global_user():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member, _ = _member(s, tenant.id)
        _, other_user = _member(s, tenant.id)
        token = _scope("phase2_mismatch")
        s.add(models.Session(token=token, tenant_id=tenant.id, member_id=member.id, user_id=other_user.id))
        s.commit()
        with pytest.raises(HTTPException) as exc:
            main.get_current_member(f"Bearer {token}", s)
        assert exc.value.status_code == 401
        assert "membership" in exc.value.detail.lower()
    finally:
        s.close()


def test_member_api_schema_never_serializes_authentication_secrets():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        member, _ = _member(s, tenant.id)
        payload = schemas.MemberOut.model_validate(member).model_dump()
        blob = json.dumps(payload, default=str)
        assert "password_hash" not in payload
        assert "google_refresh_token" not in payload
        assert "legacy-secret-hash" not in blob
        assert "refresh-secret-value" not in blob
        assert payload["google_calendar_connected"] is True
    finally:
        s.close()


def test_integration_admin_views_return_hints_not_raw_credentials(monkeypatch):
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        admin, _ = _member(s, tenant.id, role="super_admin")
        main._set_setting_value(s, "karbon_application_id_encrypted", "cipher-app")
        main._set_setting_value(s, "karbon_access_key_encrypted", "cipher-key")
        main._set_setting_value(s, "karbon_config_mode", "settings")
        s.commit()
        secrets = {"cipher-app": "application-super-secret", "cipher-key": "access-super-secret"}
        monkeypatch.setattr(main, "_decrypt_secret", lambda value: secrets[value])
        result = main.get_karbon_integration(current_member=admin, db=s)
        blob = json.dumps(result)
        assert "application-super-secret" not in blob
        assert "access-super-secret" not in blob
        assert result["application_id_hint"] == "cret"
        assert result["access_key_hint"] == "cret"
    finally:
        s.close()


def test_rate_limiter_blocks_after_limit_and_returns_retry_after():
    s = database.SessionLocal()
    try:
        identity = _scope("phase2_rate")
        main._enforce_rate_limit(s, identity, limit=2, window_seconds=900)
        main._enforce_rate_limit(s, identity, limit=2, window_seconds=900)
        with pytest.raises(HTTPException) as exc:
            main._enforce_rate_limit(s, identity, limit=2, window_seconds=900)
        assert exc.value.status_code == 429
        assert int(exc.value.headers["Retry-After"]) >= 1
    finally:
        s.close()

def test_invalid_timezone_is_rejected_server_side():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        admin, _ = _member(s, tenant.id, role="super_admin")
        target, _ = _member(s, tenant.id)
        with pytest.raises(HTTPException) as exc:
            main.update_member_timezone(
                target.id,
                schemas.MemberTimezoneUpdate(timezone_name="Definitely/Not_A_Timezone", expected_version=target.version),
                current_member=admin,
                db=s,
            )
        assert exc.value.status_code == 400
    finally:
        s.close()


def test_security_headers_and_invalid_content_length_are_enforced():
    with TestClient(main.app) as client:
        ok = client.get("/health/live")
        assert ok.headers["X-Frame-Options"] == "DENY"
        assert ok.headers["X-Content-Type-Options"] == "nosniff"
        assert "camera=()" in ok.headers["Permissions-Policy"]

        bad = client.post("/api/does-not-exist", headers={"content-length": "not-a-number"}, content=b"x")
        assert bad.status_code == 400


def test_portability_export_excludes_sessions_tokens_and_secret_settings(tmp_path, monkeypatch):
    s = database.SessionLocal()
    tenant_id = None
    try:
        tenant = _tenant(s)
        tenant_id = tenant.id
        member, user = _member(s, tenant.id)
        s.add(models.Session(token="raw-session-token", tenant_id=tenant.id, member_id=member.id, user_id=user.id))
        s.add(models.TenantSetting(tenant_id=tenant.id, key="calamari_api_key_encrypted", value="very-secret-api-key"))
        s.add(models.TenantSetting(tenant_id=tenant.id, key="display_preference", value="compact"))
        s.add(models.TenantInvitation(
            tenant_id=tenant.id,
            email="invite@example.com",
            name="Invite",
            role="member",
            token_hash="secret-invitation-token-hash",
            invited_by_id=member.id,
            expires_at=datetime.utcnow() + timedelta(days=1),
        ))
        s.commit()
    finally:
        s.close()

    module_path = Path(__file__).resolve().parents[1] / "ops" / "export_tenant.py"
    spec = importlib.util.spec_from_file_location("clockbook_export_tenant_phase2", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "tenant-export.json"
    monkeypatch.setattr(sys, "argv", [str(module_path), tenant_id, str(output)])
    module.main()

    exported = json.loads(output.read_text(encoding="utf-8"))
    blob = json.dumps(exported)
    assert "sessions" not in exported["tables"]
    assert "raw-session-token" not in blob
    assert "very-secret-api-key" not in blob
    assert "secret-invitation-token-hash" not in blob
    assert "legacy-secret-hash" not in blob
    assert "refresh-secret-value" not in blob
    assert "compact" in blob
    secret_setting = next(row for row in exported["tables"]["tenant_settings"] if row["key"] == "calamari_api_key_encrypted")
    assert secret_setting["configured"] is True
    assert "value" not in secret_setting
