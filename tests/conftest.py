import hashlib
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

TEST_DB = ROOT / "tests" / "clockbook-test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ.setdefault("CLOCKBOOK_PLATFORM_ADMIN_EMAILS", "platform@example.com")

# The execution environment used for source-only CI may not have bcrypt installed before
# the production manifest is installed. The application tests below never test password
# cryptography itself, so provide a deterministic test-only compatibility shim if needed.
try:
    import bcrypt  # noqa: F401
except ModuleNotFoundError:
    bcrypt = types.ModuleType("bcrypt")
    bcrypt.gensalt = lambda: b"test-salt"
    bcrypt.hashpw = lambda password, salt: b"test$" + hashlib.sha256(password).hexdigest().encode()
    bcrypt.checkpw = lambda password, hashed: hashed == b"test$" + hashlib.sha256(password).hexdigest().encode()
    sys.modules["bcrypt"] = bcrypt

import database  # noqa: E402
import models  # noqa: E402

models.Base.metadata.create_all(bind=database.engine)


def pytest_sessionfinish(session, exitstatus):
    database.engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()
