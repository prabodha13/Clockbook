# Clockbook

Time tracking built around clients and tasks, with a clean handoff into Karbon.

## What it does

- Standard task templates for any field of work, seeded with a bookkeeping template
- A dashboard where tasks are tracked against a client, each with its own clock
- Only one timer runs per person at a time. Starting a new one pauses the last one automatically
- Completing a task lets you add an optional note before submitting it
- An export page for turning submitted time into a Karbon ready CSV, since Karbon's public API does not currently accept time entries

## Local development

Prerequisites: Python 3.12 or newer, Node 20 or newer

### 1. Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
# Runs on http://localhost:8000
# Uses SQLite locally, clockbook.db is created automatically
```

### 2. Frontend (separate terminal)

```bash
cd frontend
npm install
npm run dev
# Runs on http://localhost:5173
# /api calls are proxied to localhost:8000
```

Open http://localhost:5173 in your browser.

## Deploy to Railway

1. Push this project to a GitHub repository, keeping the folder structure below.
2. In Railway, choose New Project, then Deploy from GitHub repo, and select the repository.
3. Add a PostgreSQL database to the project. Railway → New → Database → PostgreSQL.
   Railway sets the `DATABASE_URL` environment variable automatically.
4. Railway detects the `Dockerfile` and builds the project. No other setup is required.
5. Once the deploy finishes, open the Railway assigned domain to use the app.

Every push to the connected branch triggers a new deploy.

## Repository structure

```
backend/
  main.py
  database.py
  models.py
  schemas.py
  requirements.txt
frontend/
  src/
  package.json
  vite.config.js
  index.html
Dockerfile
railway.toml
README.md
```

## How the Karbon handoff works today

Karbon's public API can read timesheets but cannot create them, so this app cannot push time
entries into Karbon automatically. The Export page lists submitted time with the client, task,
Karbon role, Karbon task type, and duration already filled in, plus a checkbox to mark each row
once it has been re-entered into Karbon. A download and a copy button are both available so the
manual step takes seconds rather than minutes.

If Karbon later opens up write access to time entries through a partner program, this is the
one place in the code that would need to change (the export endpoints in `backend/main.py`).

## Adding a teammate

The first person to open the app is asked for their name and becomes the first team member.
Anyone with access to the app afterward can add themselves from the account menu in the top
right. There is no password. Anyone with the link can pick any name from the list, which is fine
for a small internal team but worth knowing about before sharing the link more widely. A real
login system would be a sensible next step if this ever needs proper access control.
