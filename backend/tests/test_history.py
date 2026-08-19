"""End-to-end checks for durable history, ownership and long-term memory."""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["RAG_DATA_DIR"] = tempfile.mkdtemp(prefix="rag_history_")
os.environ["RAG_EMBEDDER_BACKEND"] = "hash"
os.environ["RAG_VECTORSTORE_BACKEND"] = "memory"
os.environ["RAG_RERANKER_BACKEND"] = "lexical"
os.environ["RAG_LLM_PROVIDER"] = "extractive"
os.environ["RAG_AUTO_MIGRATE"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from api.main import app
from rag.persistence import get_chat_repository
from rag.schemas import UserContext


def check(name, condition):
    assert condition, f"FAILED: {name}"
    print(f"  ok: {name}")


alice = {"tenant_id": "demo", "user_id": "alice", "principals": ["*"]}
bob = {"tenant_id": "demo", "user_id": "bob", "principals": ["*"]}
alice_headers = {"X-Tenant-Id": "demo", "X-User-Id": "alice"}
bob_headers = {"X-Tenant-Id": "demo", "X-User-Id": "bob"}

with TestClient(app) as client:
    indexed = client.post(
        "/ingest",
        headers={**alice_headers, "X-Principals": "*"},
        json={
            "doc_id": "history-source",
            "title": "History Source",
            "acl_principals": ["*"],
            "text": "The indexed knowledge says durable citations retain their exact source text.",
        },
    )
    check("history source indexed", indexed.status_code == 200)
    created = client.post("/conversations", json={"user": alice}).json()
    conversation_id = created["id"]
    check("conversation created", bool(conversation_id) and created["title"] == "New conversation")
    check(
        "owner sees conversation",
        client.get("/conversations", headers=alice_headers).json()["conversations"][0]["id"]
        == conversation_id,
    )
    check(
        "other user cannot read conversation",
        client.get(f"/conversations/{conversation_id}", headers=bob_headers).status_code == 404,
    )

    with client.stream(
        "POST",
        "/query/stream",
        json={
            "query": "What does the indexed knowledge say?",
            "conversation_id": conversation_id,
            "user": alice,
        },
    ) as response:
        stream_text = "".join(response.iter_text())
    stream_events = [
        json.loads(line[5:].strip())
        for line in stream_text.splitlines()
        if line.startswith("data:") and line[5:].strip() not in {"", "{}"}
    ]
    detail = client.get(f"/conversations/{conversation_id}", headers=alice_headers).json()
    check("stream persisted both messages", [m["role"] for m in detail["messages"]] == ["user", "assistant"])
    check("reloaded source keeps cited chunk text", any(
        source.get("text")
        for message in detail["messages"] if message["role"] == "assistant"
        for source in message.get("sources", [])
    ))
    check("stream placeholder completed", detail["messages"][-1]["status"] == "complete")
    check(
        "first-intent title generated",
        detail["title"] == "Indexed knowledge",
    )
    initial_title = detail["title"]
    assistant_id = detail["messages"][-1]["id"]
    initial_final = next(event for event in stream_events if event.get("type") == "final")
    check("stream exposes durable assistant id", initial_final["message_id"] == assistant_id)

    with client.stream(
        "POST",
        "/query/stream",
        json={
            "query": "This replacement body must not become a new user message.",
            "conversation_id": conversation_id,
            "regenerate_message_id": assistant_id,
            "user": alice,
        },
    ) as response:
        regenerated_text = "".join(response.iter_text())
    regenerated_events = [
        json.loads(line[5:].strip())
        for line in regenerated_text.splitlines()
        if line.startswith("data:") and line[5:].strip() not in {"", "{}"}
    ]
    regenerated_final = next(
        event for event in regenerated_events if event.get("type") == "final"
    )
    regenerated_detail = client.get(
        f"/conversations/{conversation_id}", headers=alice_headers
    ).json()
    check("regenerate bypasses cached answer", any(
        event.get("type") == "progress"
        and event.get("stage") == "cache"
        and event.get("label") == "Generating a fresh answer"
        for event in regenerated_events
    ))
    check("regenerate keeps the same assistant id", regenerated_final["message_id"] == assistant_id)
    check("regenerate does not duplicate turns", len(regenerated_detail["messages"]) == 2)
    check(
        "regenerate preserves the original user prompt",
        regenerated_detail["messages"][0]["content"] == "What does the indexed knowledge say?",
    )
    check(
        "other user cannot regenerate answer",
        client.post(
            "/query/stream",
            headers=bob_headers,
            json={
                "query": "ignored",
                "conversation_id": conversation_id,
                "regenerate_message_id": assistant_id,
                "user": bob,
            },
        ).status_code == 404,
    )

    before_summary_count = len(detail["messages"])
    summary_response = client.post(
        f"/conversations/{conversation_id}/summary", headers=alice_headers
    )
    check("owner can summarize full chat", summary_response.status_code == 200)
    summary = summary_response.json()
    check("summary reports complete transcript size", summary["message_count"] == 2)
    check("summary returns useful text", bool(summary["summary"].strip()))
    after_summary = client.get(
        f"/conversations/{conversation_id}", headers=alice_headers
    ).json()
    check("summary does not create a chat message", len(after_summary["messages"]) == before_summary_count)
    check(
        "other user cannot summarize conversation",
        client.post(f"/conversations/{conversation_id}/summary", headers=bob_headers).status_code
        == 404,
    )

    client.post(
        "/query",
        json={
            "query": "Please remember that concise Vietnamese answers are preferred",
            "conversation_id": conversation_id,
            "user": alice,
        },
    )
    title_after_later_topic = client.get(
        f"/conversations/{conversation_id}", headers=alice_headers
    ).json()["title"]
    check("automatic title stays stable after later turns", title_after_later_topic == initial_title)
    memory_list = client.get("/memories", headers=alice_headers).json()["memories"]
    check("explicit chat request creates memory", len(memory_list) == 1)
    check("memory content is clean", memory_list[0]["content"] == "concise Vietnamese answers are preferred")
    check("memory is owner scoped", client.get("/memories", headers=bob_headers).json()["memories"] == [])

    manual = client.post(
        "/memories",
        json={"kind": "fact", "content": "Works in the support team", "user": alice},
    ).json()
    check("manual memory created", manual["is_explicit"] is True)
    duplicate = client.post(
        "/memories",
        json={"kind": "fact", "content": "  Works   in the support team  ", "user": alice},
    ).json()
    check("duplicate memory upserts", duplicate["id"] == manual["id"])
    other = client.post(
        "/memories",
        json={"kind": "preference", "content": "Use bullet points", "user": alice},
    ).json()
    check(
        "memory update collision is validation error",
        client.patch(
            f"/memories/{other['id']}",
            json={"kind": "fact", "content": "Works in the support team", "user": alice},
        ).status_code
        == 422,
    )
    check(
        "secret-like memory rejected",
        client.post(
            "/memories", json={"kind": "fact", "content": "API key is abc", "user": alice}
        ).status_code
        == 422,
    )
    check(
        "memory deletion works",
        client.delete(f"/memories/{manual['id']}", headers=alice_headers).status_code == 204,
    )

    updated = client.patch(
        f"/conversations/{conversation_id}",
        json={"title": "Pinned chat", "pinned": True, "user": alice},
    ).json()
    check("rename and pin persist", updated["title"] == "Pinned chat" and updated["pinned"] is True)
    client.post(
        "/query",
        json={
            "query": "Discuss a new topic after my manual rename",
            "conversation_id": conversation_id,
            "user": alice,
        },
    )
    renamed_detail = client.get(
        f"/conversations/{conversation_id}", headers=alice_headers
    ).json()
    check("manual title stays locked after later turns", renamed_detail["title"] == "Pinned chat")
    get_chat_repository().backfill_auto_titles()
    backfilled_detail = client.get(
        f"/conversations/{conversation_id}", headers=alice_headers
    ).json()
    check("automatic-title backfill preserves manual names", backfilled_detail["title"] == "Pinned chat")

    check(
        "soft delete succeeds",
        client.delete(f"/conversations/{conversation_id}", headers=alice_headers).status_code == 204,
    )
    check("soft-deleted hidden", client.get("/conversations", headers=alice_headers).json()["conversations"] == [])
    deleted = client.get("/conversations?deleted=true", headers=alice_headers).json()["conversations"]
    check("deleted item has purge date", len(deleted) == 1 and deleted[0]["purge_after"])
    restored = client.post(
        f"/conversations/{conversation_id}/restore", headers=alice_headers
    ).json()
    check("restore succeeds", restored["deleted_at"] is None)

    repository = get_chat_repository()
    alice_context = UserContext(
        tenant_id="demo", user_id="alice", principals=["*"]
    )
    interrupted = repository.create_conversation(alice_context)
    turn = repository.start_turn(alice_context, interrupted["id"], "Interrupted request")
    repository.fail_turn(
        alice_context, interrupted["id"], turn["assistant_message"]["id"]
    )
    interrupted_detail = repository.get_conversation(alice_context, interrupted["id"])
    check(
        "interrupted turn is finalized as error",
        [message["status"] for message in interrupted_detail["messages"]]
        == ["complete", "error"],
    )

    expired = repository.create_conversation(alice_context)
    repository.soft_delete_conversation(
        alice_context, expired["id"], retention_days=0
    )
    check(
        "expired deleted conversation is hidden",
        expired["id"]
        not in {
            item["id"]
            for item in repository.list_conversations(alice_context, deleted=True)
        },
    )
    try:
        repository.restore_conversation(alice_context, expired["id"])
    except LookupError:
        expired_restore_rejected = True
    else:
        expired_restore_rejected = False
    check("expired conversation cannot be restored", expired_restore_rejected)

print("\nALL HISTORY ASSERTIONS PASSED")
