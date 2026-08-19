"""FastAPI service package for the FPT RAG backend.

Run with:
    uvicorn api.main:app --reload --port 8000

Offline (no models / no LLM key):
    RAG_EMBEDDER_BACKEND=hash RAG_VECTORSTORE_BACKEND=chroma \
    RAG_LLM_PROVIDER=extractive uvicorn api.main:app --port 8000
"""

from .main import app

__all__ = ["app"]
