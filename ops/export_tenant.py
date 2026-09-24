#!/usr/bin/env python3
"""Export one ClockBook tenant's non-secret business data as JSON.

Usage:
    DATABASE_URL=postgresql://... python ops/export_tenant.py tenant_xxx output.json

This is an operational portability tool, not a backup. It deliberately excludes sessions,
OAuth state, password hashes, integration tokens/keys and invitation token hashes.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import MetaData, Table, select

from database import engine

EXCLUDED_TABLES = {
    "sessions", "google_oauth_states", "rate_limit_buckets", "login_events", "clock_start_events"
}
SECRET_COLUMNS = {
    "password_hash", "google_refresh_token", "token_hash", "access_key", "api_key", "token"
}
SECRET_SETTING_MARKERS = ("secret", "token", "key", "password", "credential")


def json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: export_tenant.py <tenant_id> <output.json>")
    tenant_id, output_name = sys.argv[1], sys.argv[2]
    metadata = MetaData()
    metadata.reflect(bind=engine)
    exported = {"tenant_id": tenant_id, "exported_at": datetime.utcnow().isoformat() + "Z", "tables": {}}

    with engine.connect() as conn:
        tenants = metadata.tables.get("tenants")
        if tenants is None:
            raise SystemExit("tenants table not found")
        tenant = conn.execute(select(tenants).where(tenants.c.id == tenant_id)).mappings().first()
        if not tenant:
            raise SystemExit("Tenant not found")
        exported["tenant"] = {k: json_value(v) for k, v in tenant.items() if k not in SECRET_COLUMNS}

        for name, table in sorted(metadata.tables.items()):
            if name in EXCLUDED_TABLES or "tenant_id" not in table.c or name == "tenants":
                continue
            rows = conn.execute(select(table).where(table.c.tenant_id == tenant_id)).mappings().all()
            cleaned = []
            for row in rows:
                item = {}
                for key, value in row.items():
                    if key in SECRET_COLUMNS:
                        continue
                    if name == "tenant_invitations" and key == "token_hash":
                        continue
                    if name == "tenant_settings" and key == "value":
                        setting_key = str(row.get("key") or "").lower()
                        if any(marker in setting_key for marker in SECRET_SETTING_MARKERS):
                            item["configured"] = bool(value)
                            continue
                    item[key] = json_value(value)
                cleaned.append(item)
            exported["tables"][name] = cleaned

    out = Path(output_name)
    out.write_text(json.dumps(exported, indent=2, default=json_value), encoding="utf-8")
    print(f"Exported tenant {tenant_id} to {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
