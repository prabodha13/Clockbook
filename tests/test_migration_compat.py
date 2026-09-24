from pathlib import Path

from sqlalchemy import create_engine, inspect, text

import main


def test_hardening_migration_upgrades_preexisting_client_table(tmp_path, monkeypatch):
    """CI proof that the compatibility migration can upgrade an older existing schema."""
    db_path = Path(tmp_path) / "legacy.db"
    legacy_engine = create_engine(f"sqlite:///{db_path}")
    with legacy_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE clients ("
            "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, name VARCHAR NOT NULL, code VARCHAR)"
        ))
        conn.execute(text(
            "INSERT INTO clients (id, tenant_id, name, code) VALUES "
            "('legacy_client', 'tenant_legacy', 'Legacy Client', 'LEG1')"
        ))
        conn.execute(text(
            "CREATE TABLE audit_events ("
            "id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL, created_at DATETIME, "
            "actor_member_id VARCHAR, action VARCHAR, entity_type VARCHAR, entity_id VARCHAR, changes JSON)"
        ))

    monkeypatch.setattr(main, "engine", legacy_engine)
    main.run_hardening_migrations()

    columns = {c["name"] for c in inspect(legacy_engine).get_columns("clients")}
    assert "version" in columns
    with legacy_engine.connect() as conn:
        row = conn.execute(text("SELECT id, name, version FROM clients WHERE id='legacy_client'" )).first()
        assert row == ("legacy_client", "Legacy Client", 1)
        indexes = {idx["name"] for idx in inspect(legacy_engine).get_indexes("audit_events")}
        assert "ix_audit_events_tenant_created" in indexes
        assert "ix_audit_events_tenant_entity" in indexes
