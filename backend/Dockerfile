# FPT RAG API — multi-stage container image (non-root, no build toolchain in the
# final layer).
#
# Build the FULL image (local BGE-M3 + reranker; large, needs model downloads):
#   docker build -t fpt-rag-api .
#
# Build the SLIM image for a guardrailed VDI (no local models; embeddings + LLM
# via the aiportalapi gateway):
#   docker build --build-arg REQUIREMENTS=requirements-vdi-minimal.txt -t fpt-rag-api .

# ---- builder: compile wheels into an isolated venv -------------------------
FROM python:3.12-slim AS builder

ARG REQUIREMENTS=requirements.txt
ENV PIP_NO_CACHE_DIR=1

# The full local-model profile may need a compiler for a dependency without a
# wheel. The slim/VDI profile is wheel-only and deliberately skips Debian's
# package mirror (often blocked on corporate networks).
RUN if [ "${REQUIREMENTS}" = "requirements.txt" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends build-essential \
      && rm -rf /var/lib/apt/lists/*; \
    fi

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY requirements*.txt ./
RUN pip install -r ${REQUIREMENTS}

# ---- runtime: copy the venv, drop the toolchain, run as a non-root user -----
FROM python:3.12-slim AS runtime

ARG INSTALL_OCR=true

ENV PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    RAG_DATA_DIR=/data

# Tesseract stays local to the container so scanned documents do not need to be
# sent to an external vision service. Set --build-arg INSTALL_OCR=false only on
# a VDI whose image build cannot reach the Debian package mirror.
RUN if [ "${INSTALL_OCR}" = "true" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends \
           tesseract-ocr tesseract-ocr-eng tesseract-ocr-vie \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
 && useradd -r -u 10001 -m -d /home/app app \
 && mkdir -p /data /app \
 && chown -R app:app /data /app

COPY --from=builder --chown=app:app /venv /venv
WORKDIR /app
COPY --chown=app:app . .

USER app
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
