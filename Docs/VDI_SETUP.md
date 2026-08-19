# CPU-only VDI deployment

The VDI does not need GitHub or a pre-copied project folder. On the source
machine, regenerate `Docs/bootstrap_project.py` after the final code changes:

```powershell
cd C:\path\to\FPT_RAG
python Docs\build_bootstrap_bundle.py
```

Transfer that single generated `bootstrap_project.py` file through the approved
company channel. It contains the complete frontend, backend, and project
documentation tree. It excludes secrets, installed packages, local virtual
environments, and runtime indexes; those are recreated on the VDI.

Prerequisite: 64-bit Python 3.11+ available on `PATH`. If Node.js/npm is not
installed, the bootstrap downloads and checksum-verifies the latest portable
Node.js LTS release inside `C:\FPT_RAG\.tools`. No administrator rights, Docker,
GPU, CUDA, Torch, or local model download is required.

```powershell
cd C:\path\on\the\VDI
python .\bootstrap_project.py --destination C:\FPT_RAG --profile gateway
```

The bootstrap recreates the source tree at `C:\FPT_RAG`, creates
`backend/.venv`, installs `backend/requirements-vdi-minimal.txt`, runs `npm ci`
from the locked frontend dependency tree, builds the frontend, and runs the
offline backend test suite. It creates `backend/.env` from the selected profile.

For the gateway profile, edit `backend/.env` and replace `YOUR-AIPORTAL-HOST` plus `REPLACE_WITH_YOUR_GATEWAY_KEY`. The embedding endpoint must support the selected embedding model and its returned vector dimension must match `RAG_EMBEDDING_DIM` (1536 by default). If the gateway uses different models or dimensions, update all related values before the first ingest. Use a new `RAG_INDEX_VERSION` when changing an embedding model or dimension, then re-ingest all documents.

For an initial installation without gateway credentials, select
`--profile offline`. That mode remains CPU-only but intentionally uses hash
embeddings and extractive answers.

Start the services in two terminals:

```powershell
C:\FPT_RAG\start-backend.cmd
```

```powershell
C:\FPT_RAG\start-frontend.cmd
```

Open `http://127.0.0.1:5173`. Vite proxies `/api/*` to the backend on port 8000.
