# Stage 1: build the React frontend
FROM node:20-alpine AS frontend

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# Outputs to /app/backend/dist, see vite.config.js outDir
RUN npm run build


# Stage 2: Python backend
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Keep the existing runtime layout: backend files live directly in /app.
COPY backend/ ./

# Also keep a backend/ package copy because alembic/env.py expects /app/backend.
COPY backend/ ./backend/

# Include Alembic configuration and migrations in the production image.
COPY alembic.ini ./alembic.ini
COPY alembic/ ./alembic/

# Copy the built React app into the backend folder used by the running app.
COPY --from=frontend /app/backend/dist ./dist

# Railway injects $PORT at runtime
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
