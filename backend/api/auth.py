"""Request identity resolution for the ACL checks.

Header-first, body-fallback (per the chosen design): SSO/proxy headers
injected by nginx at the edge win; the request body's ``user`` object is a
fallback for local dev and direct calls. Either way the resulting
:class:`rag.UserContext` is what drives CP1/CP2/CP3.

Header contract (as injected by the edge proxy after SSO):
  X-Tenant-Id, X-User-Id, X-Principals (comma-separated), X-Classification,
  X-Session-Id, X-Language

Hardening knobs (env):
- ``RAG_PROXY_SHARED_SECRET`` — when set, identity headers are honored only if
  the request also carries a matching ``X-Proxy-Secret`` header (injected by
  the edge proxy). A request that presents identity headers with a missing or
  wrong secret is rejected outright — never silently downgraded to the body.
- ``RAG_ALLOW_BODY_IDENTITY`` (default ``true``) — set ``false`` in production
  so unauthenticated callers cannot claim an identity via the request body.

Identity is resolved from ONE source atomically: if identity headers are
present, all fields come from headers; otherwise all come from the body.
Mixing per-field would let a caller top up a proxy-asserted identity with
extra body principals.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

from rag import UserContext

from .schemas import UserBody

_VALID_CLASS = {"public", "internal", "confidential", "restricted"}


def _split(raw: str | None) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()] if raw else []


def _body_identity_allowed() -> bool:
    raw = os.environ.get("RAG_ALLOW_BODY_IDENTITY", "true")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_user(request: Request, body_user: UserBody | None) -> UserContext:
    h = request.headers
    has_header_identity = bool(h.get("X-Tenant-Id") or h.get("X-User-Id"))

    if has_header_identity:
        secret = os.environ.get("RAG_PROXY_SHARED_SECRET")
        if secret and not hmac.compare_digest(h.get("X-Proxy-Secret") or "", secret):
            raise HTTPException(
                status_code=401,
                detail="Identity headers present but the proxy secret is missing or invalid.",
            )
        tenant_id = h.get("X-Tenant-Id")
        user_id = h.get("X-User-Id")
        principals = _split(h.get("X-Principals"))
        classification = h.get("X-Classification") or "internal"
        session_id = h.get("X-Session-Id")
        language = h.get("X-Language")
    else:
        b = body_user or UserBody()
        if (b.tenant_id or b.user_id) and not _body_identity_allowed():
            raise HTTPException(
                status_code=401,
                detail="Body-supplied identity is disabled (RAG_ALLOW_BODY_IDENTITY=false).",
            )
        tenant_id = b.tenant_id
        user_id = b.user_id
        principals = b.principals
        classification = b.classification
        session_id = b.session_id
        language = b.language

    if not tenant_id or not user_id:
        raise HTTPException(
            status_code=400,
            detail="Missing user identity: provide X-Tenant-Id/X-User-Id headers "
            "or a `user` object with tenant_id and user_id in the body.",
        )
    if classification not in _VALID_CLASS:
        raise HTTPException(status_code=400, detail=f"Invalid classification: {classification}")

    return UserContext(
        tenant_id=tenant_id,
        user_id=user_id,
        principals=principals,
        max_classification=classification,  # type: ignore[arg-type]
        session_id=session_id,
        language=language,
    )


def require_admin(user: UserContext) -> None:
    """Gate for the ingest/delete endpoints. ``RAG_ADMIN_PRINCIPALS`` is a
    comma-separated allowlist of principals permitted to write the index;
    unset means the gate is open (local dev). Set it in production."""
    allowed = _split(os.environ.get("RAG_ADMIN_PRINCIPALS"))
    if allowed and not set(allowed) & set(user.all_principals()):
        raise HTTPException(
            status_code=403,
            detail="Not authorized for document management (RAG_ADMIN_PRINCIPALS).",
        )
