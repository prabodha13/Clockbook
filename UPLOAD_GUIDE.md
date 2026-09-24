# ClockBook GitHub upload guide

This package is arranged to match the repository structure shown in your screenshots.

## Replace these existing application files

- `backend/main.py`
- `backend/models.py`
- `backend/schemas.py`
- `backend/database.py`
- `backend/MULTITENANCY_DEPLOYMENT.md`
- `frontend/src/App.jsx`
- `frontend/src/api.js`

## Add these hardening folders/files at repository root

- `.github/`
- `tests/`
- `ops/`
- `alembic/`
- `alembic.ini`
- `requirements-ci.txt`
- the hardening `.md` files in this package
- `clockbook-client-import-template.csv` (optional repository reference file)

## Do NOT replace these existing files/folders

Keep your existing repository versions of:

- `backend/requirements.txt`
- `frontend/package.json`
- `frontend/package-lock.json`
- `frontend/vite.config.js`
- `frontend/index.html`
- `frontend/public/`
- root `Dockerfile`
- root `README.md`
- root `railway.toml`

This package intentionally does not contain those files.

## Important Alembic note

The Alembic files in this package have been adapted for your real repository layout. They add `backend/` to the Python import path before importing `database` and `models`.

The baseline revision is intentionally schema-neutral. Follow `MIGRATIONS.md` before stamping a live database. Do not blindly run `alembic upgrade head` as a replacement for the documented baseline adoption procedure.

## Safest upload order

1. Commit or download a backup of the current GitHub repository.
2. Upload/replace `backend/` application files.
3. Upload/replace `frontend/src/App.jsx` and `frontend/src/api.js`.
4. Add `.github/`, `tests/`, `ops/`, `alembic/`, `alembic.ini`, and `requirements-ci.txt`.
5. Add the root hardening documentation.
6. Let GitHub/Railway build and verify the deployment before making database migration changes.

Do not upload `__pycache__`, `.pytest_cache`, or `.pyc` files. None are included here.
