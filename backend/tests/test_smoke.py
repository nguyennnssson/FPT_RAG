"""End-to-end smoke test using offline fallback backends (Milestone M2)."""
import os, sys, tempfile
from pathlib import Path

os.environ["RAG_DATA_DIR"] = tempfile.mkdtemp(prefix="rag_smoke_")
os.environ["RAG_EMBEDDER_BACKEND"] = "hash"
os.environ["RAG_VECTORSTORE_BACKEND"] = "memory"
os.environ["RAG_LLM_PROVIDER"] = "extractive"
os.environ["RAG_RERANKER_BACKEND"] = "lexical"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag import Ingester, RAGConfig, RAGPipeline, SourceDocument, UserContext

cfg = RAGConfig.load()

DOC = """# Refund Policy

## Standard consumer refunds
Refunds for US retail purchases are available within 30 calendar days of purchase.
Customers must present a valid receipt to receive a refund.

## Enterprise agreements
Enterprise customers are governed by their master service agreement, not the
standard 30-day window. Priority support is included for all enterprise tiers.
"""

doc = SourceDocument(
    tenant_id="demo", doc_id="refund-policy", source_system="manual",
    source_uri="https://intranet/policies/refunds", title="Refund Policy",
    content_type="text/markdown", text=DOC, acl_principals=["group:support"],
    sensitivity="internal",
)

ing = Ingester(cfg)
result = ing.ingest(doc)
print("INGEST:", result.status, "chunks=", result.chunks_indexed)
assert result.status == "indexed" and result.chunks_indexed > 0
assert ing.ingest(doc).status == "skipped_unchanged"   # idempotency

user = UserContext(tenant_id="demo", user_id="alice", principals=["group:support"],
                   max_classification="internal", session_id="s1")
pipe = RAGPipeline(cfg)

ans = pipe.query("What is the refund window for US retail purchases?", user)
print("Q1 answered:", not ans.abstained, "| sources:", [s.label for s in ans.sources])
assert not ans.abstained and ans.sources

# M4 proof: repeat question is a cache hit — with NO Redis (in-process cache).
ans_repeat = pipe.query("What is the refund window for US retail purchases?", user)
assert ans_repeat.metadata.get("cache_hit") is True, "repeat query should hit the serverless cache"
print("Q1 repeat cache_hit:", ans_repeat.metadata.get("cache_hit"))

outsider = UserContext(tenant_id="demo", user_id="bob", principals=["group:sales"], max_classification="internal")
assert pipe.query("refund window US retail", outsider).abstained          # ACL denial

other = UserContext(tenant_id="acme", user_id="carol", principals=["group:support"], max_classification="internal")
assert pipe.query("refund window US retail", other).abstained             # cross-tenant

d = ing.delete("demo", "refund-policy")
print("DELETE:", d.status)
assert pipe.query("What is the refund window for US retail purchases?", user).abstained  # tombstoned

print("\nALL SMOKE ASSERTIONS PASSED")
