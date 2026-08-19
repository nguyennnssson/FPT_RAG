"""In-process test of the FastAPI service (offline backends)."""
import os, sys, tempfile, json
from pathlib import Path

os.environ["RAG_DATA_DIR"] = tempfile.mkdtemp(prefix="rag_api_")
os.environ["RAG_EMBEDDER_BACKEND"] = "hash"
os.environ["RAG_VECTORSTORE_BACKEND"] = "memory"
os.environ["RAG_RERANKER_BACKEND"] = "lexical"
os.environ["RAG_LLM_PROVIDER"] = "extractive"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from api.main import app, _citation_status_answer

c = TestClient(app)

def check(name, cond):
    assert cond, f"FAILED: {name}"
    print(f"  ok: {name}")

r = c.get("/health")
check("health 200", r.status_code == 200)

hdr = {"X-Tenant-Id": "demo", "X-User-Id": "admin", "X-Principals": "group:support", "X-Classification": "internal"}
r = c.post("/ingest", headers=hdr, json={
    "doc_id": "refund-policy", "title": "Refund Policy", "content_type": "text/markdown",
    "acl_principals": ["group:support"], "sensitivity": "internal",
    "text": "# Refund Policy\n\n## US refunds\nRefunds for US retail purchases are available within 30 calendar days of purchase.",
})
check("ingest indexed", r.status_code == 200 and r.json()["status"] == "indexed")

docs = c.get("/documents", headers=hdr)
check("document list has ingestion metadata", docs.status_code == 200 and {
    "folder": "API", "file_type": "MD", "status": "active",
}.items() <= docs.json()["documents"][0].items())

preview = c.get("/documents/refund-policy/preview", headers=hdr)
check("authorized document preview", preview.status_code == 200
      and "30 calendar days" in preview.json()["content"])
denied_preview = c.get(
    "/documents/refund-policy/preview",
    headers={**hdr, "X-User-Id": "bob", "X-Principals": "group:sales"},
)
check("document preview enforces ACL", denied_preview.status_code == 404)

# The local demo fallback represents an ordinary employee. It can read both
# explicitly world-readable and all-employee documents, but is neither a
# manager nor a security operator and remains capped at INTERNAL.
for doc_id, acl, sensitivity, marker in [
    ("demo-world", ["*"], "internal", "worldcanary"),
    ("demo-employee", ["group:all-employees"], "internal", "employeecanary"),
    ("demo-manager", ["group:people-managers"], "confidential", "managercanary"),
    ("demo-security", ["group:security-operations"], "restricted", "securitycanary"),
]:
    response = c.post("/ingest", headers=hdr, json={
        "doc_id": doc_id, "title": doc_id, "content_type": "text/markdown",
        "acl_principals": acl, "sensitivity": sensitivity,
        "text": f"# {doc_id}\n\nThe unique retrieval marker is {marker} for access testing.",
    })
    check(f"ingest {doc_id}", response.status_code == 200)

def demo_search(query):
    return c.post("/search", json={"query": query, "top_k": 10}).json()["results"]

check("demo fallback reads world-readable docs",
      any(item["doc_id"] == "demo-world" for item in demo_search("worldcanary")))
check("demo fallback reads all-employee docs",
      any(item["doc_id"] == "demo-employee" for item in demo_search("employeecanary")))
check("demo fallback cannot retrieve manager-confidential docs",
      all(item["doc_id"] != "demo-manager" for item in demo_search("managercanary")))
check("demo fallback cannot retrieve security-restricted docs",
      all(item["doc_id"] != "demo-security" for item in demo_search("securitycanary")))

# Multipart uploads retain their original bytes for an ACL-checked faithful
# preview/download while the index continues to use extracted text.
upload_bytes = b"# Stored original\n\nThe retained source says hello to the preview endpoint."
uploaded = c.post(
    "/upload",
    headers=hdr,
    data={"acl": "group:support"},
    files={"file": ("stored-original.txt", upload_bytes, "text/plain")},
)
check("uploaded original indexed", uploaded.status_code == 200 and uploaded.json()["status"] == "indexed")
stored_preview = c.get("/documents/stored-original.txt/preview", headers=hdr)
check(
    "preview advertises retained original",
    stored_preview.status_code == 200
    and stored_preview.json()["has_original"] is True
    and stored_preview.json()["file_name"] == "stored-original.txt",
)
stored_file = c.get("/documents/stored-original.txt/file", headers=hdr)
check(
    "authorized original bytes round-trip",
    stored_file.status_code == 200 and stored_file.content == upload_bytes,
)
denied_file = c.get(
    "/documents/stored-original.txt/file",
    headers={**hdr, "X-User-Id": "bob", "X-Principals": "group:sales"},
)
check("original file enforces ACL", denied_file.status_code == 404)

r = c.post("/query", json={"query": "What is the refund window for US retail purchases?",
    "user": {"tenant_id": "demo", "user_id": "alice", "principals": ["group:support"], "classification": "internal"}})
check("query answered", r.status_code == 200 and r.json()["abstained"] is False and r.json()["sources"])
check("query source includes cited chunk text",
      "30 calendar days" in r.json()["sources"][0].get("text", ""))

# Chat attachment ids scope both dense and keyword retrieval to the uploaded
# file. This prevents an attached screenshot from being acknowledged in text
# while the answer is actually grounded in an unrelated global document.
for doc_id, marker in [
    ("attached-image.png", "attachedvisualcanary"),
    ("unrelated-global", "globaldocumentcanary"),
]:
    attached = c.post("/ingest", headers=hdr, json={
        "doc_id": doc_id, "title": doc_id, "content_type": "text/plain",
        "acl_principals": ["group:support"], "sensitivity": "internal",
        "text": f"The diagnostic marker in this document is {marker}.",
    })
    check(f"ingest {doc_id}", attached.status_code == 200)

scoped = c.post("/query", headers=hdr, json={
    "query": "What diagnostic marker appears in the attached image?",
    "attachment_doc_ids": ["attached-image.png"],
})
check(
    "attachment query is grounded only in attached document",
    scoped.status_code == 200
    and scoped.json()["abstained"] is False
    and {source["doc_id"] for source in scoped.json()["sources"]}
        == {"attached-image.png"},
)
check(
    "invalid attachment document id is rejected",
    c.post("/query", headers=hdr, json={
        "query": "inspect attachment", "attachment_doc_ids": ["../secret"],
    }).status_code == 422,
)

meta_answer = _citation_status_answer(
    "Tại sao không có source 3 trước khi đến source 4?",
    [{
        "role": "assistant", "status": "complete", "content": "old answer",
        "sources": [{"label": "S1"}, {"label": "S2"}, {"label": "S4"}],
    }],
)
check(
    "citation UI follow-up uses prior message metadata instead of document search",
    meta_answer is not None
    and "S1, S2, S4" in meta_answer.answer
    and meta_answer.sources == [],
)

r = c.post("/query", headers={"X-Tenant-Id": "demo", "X-User-Id": "bob", "X-Principals": "group:sales"},
           json={"query": "refund window US retail"})
check("ACL denial abstains", r.json()["abstained"] is True)

check("missing identity -> 400", c.post("/query", json={"query": "hi"}).status_code == 400)

with c.stream("POST", "/query/stream", headers=hdr, json={"query": "refund window US retail purchases"}) as r:
    body = "".join(r.iter_text())
events = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ") and l[6:].strip() not in ("", "{}")]
progress = [e for e in events if e.get("type") == "progress"]
check("SSE reports retrieval progress", any(e.get("stage") == "retrieval" for e in progress))
check("SSE reports generation progress", any(e.get("stage") == "generation" for e in progress))
check("SSE has deltas", any(e.get("type") == "delta" for e in events))
check("SSE has one final with sources", len([e for e in events if e.get("type") == "final"]) == 1)
check("SSE final includes cited chunk text", any(
    "30 calendar days" in source.get("text", "")
    for event in events if event.get("type") == "final"
    for source in event.get("sources", [])
))

# A pipeline/provider exception is a terminal SSE error event, not a truncated
# HTTP 200 response that leaves the browser waiting forever.
from api import main as _api_main
_original_query_stream = _api_main._pipeline.query_stream
def _failing_stream(*_args, **_kwargs):
    yield {
        "type": "progress", "stage": "generation", "status": "running",
        "label": "Running language model", "detail": "safe command summary",
        "detail_type": "command", "elapsed_ms": 1,
    }
    raise RuntimeError("provider unavailable")
_api_main._pipeline.query_stream = _failing_stream
try:
    with c.stream("POST", "/query/stream", headers=hdr,
                  json={"query": "force provider failure"}) as r:
        error_body = "".join(r.iter_text())
finally:
    _api_main._pipeline.query_stream = _original_query_stream
error_events = [json.loads(l[6:]) for l in error_body.splitlines()
                if l.startswith("data: ") and l[6:].strip() not in ("", "{}")]
check("SSE converts provider exception to error event",
      len([e for e in error_events if e.get("type") == "error"]) == 1)

# A long answer proves truncation: beyond 500 chars must not be persisted.
_long_answer = "HEAD " + ("z" * 700) + " TAILMARKER [S1]"
fb = c.post("/feedback", headers=hdr, json={"query": "refund window", "rating": "down",
            "answer": _long_answer, "comment": "missed detail"})
check("feedback recorded", fb.status_code == 200 and fb.json()["status"] == "recorded")

# M-2: feedback field caps + answer stored as hash + bounded excerpt, not raw.
check("oversized feedback query -> 422",
      c.post("/feedback", headers=hdr, json={"query": "x" * 5000, "rating": "up"}).status_code == 422)
import os as _os, glob as _glob
fbfile = _glob.glob(_os.path.join(_os.environ["RAG_DATA_DIR"], "feedback", "feedback.jsonl"))
fbtext = open(fbfile[0], encoding="utf-8").read() if fbfile else ""
check("feedback stores answer hash", "answer_sha256" in fbtext)
check("feedback truncates long answer (no full raw)", "TAILMARKER" not in fbtext)

# M-3: ingest size cap + path-safe doc_id.
check("oversized ingest text -> 422",
      c.post("/ingest", headers=hdr, json={"doc_id": "big", "acl_principals": ["*"],
             "text": "x" * 2_000_001}).status_code == 422)
check("path-traversal doc_id -> 422",
      c.post("/ingest", headers=hdr, json={"doc_id": "../etc/passwd", "acl_principals": ["*"],
             "text": "hello"}).status_code == 422)

m = c.get("/metrics").json()
check("metrics has decisions", "answered" in m["decisions"] and m["total_requests"] >= 2)
check("metrics has latency percentiles", "p95" in m["latency_ms"])
check("metrics has rates", "abstention" in m["rates"] and "citation_valid" in m["rates"])

# M-7: ops endpoints are admin-gated once RAG_ADMIN_PRINCIPALS is set.
_os.environ["RAG_ADMIN_PRINCIPALS"] = "group:rag-admin"
try:
    check("metrics 403 without admin", c.get("/metrics", headers=hdr).status_code == 403)
    check("health minimal without admin", set(c.get("/health", headers=hdr).json()) == {"status", "version"})
    admin_hdr = {**hdr, "X-Principals": "group:rag-admin"}
    check("metrics 200 with admin", c.get("/metrics", headers=admin_hdr).status_code == 200)
    check("health full with admin", "bm25_docs" in c.get("/health", headers=admin_hdr).json())
finally:
    del _os.environ["RAG_ADMIN_PRINCIPALS"]

r = c.delete("/ingest/refund-policy", headers=hdr)
check("delete tombstone", r.status_code == 200 and r.json()["status"] == "deleted")
check("post-delete abstain", c.post("/query", headers=hdr, json={"query": "refund window US retail"}).json()["abstained"] is True)
check(
    "deleting upload removes original bytes",
    c.delete("/ingest/stored-original.txt", headers=hdr).status_code == 200
    and c.get("/documents/stored-original.txt/file", headers=hdr).status_code == 404,
)

print("\nALL API ASSERTIONS PASSED")
