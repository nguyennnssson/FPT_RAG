# FPT RAG Workspace

Bilingual retrieval-augmented generation workspace with a
FastAPI backend and a React/Vite frontend.

## Prerequisites

- Python 3.11 or newer
- Node.js and npm

## Backend setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-vdi-minimal.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe tests\run_all.py
.\.venv\Scripts\python.exe -m uvicorn api.main:app --port 8000
```

The minimal profile avoids downloading the multi-gigabyte local model stack.
See `backend/README.md` and `backend/.env.example` for offline, local-model, and
company-gateway configurations.

## Frontend setup

In a second terminal:

```powershell
cd frontend
npm ci
npm test
npm run dev
```

Open <http://localhost:5173>. The Vite development server proxies `/api` to the
backend at `http://127.0.0.1:8000`.

After both dependency sets have been installed, `start-system.cmd` can launch
the backend and frontend together.

## Validation

```powershell
cd backend
.\.venv\Scripts\python.exe tests\run_all.py
.\.venv\Scripts\python.exe eval\security_pass.py

cd ..\frontend
npm test
npm run build
```

Local environment files, installed dependencies, generated indexes, logs,
databases, private corpora, and backups are intentionally excluded from Git.
Each teammate recreates those from the committed manifests and examples.
