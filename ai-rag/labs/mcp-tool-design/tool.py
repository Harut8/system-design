"""invite_user as a transport-agnostic MCP tool handler: dict in, CallToolResult dict out."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from fields import FieldError, lookup, normalize_display_name, normalize_email, normalize_languages

SUPPORTED_LANGUAGES = {"en", "hy", "ru", "es", "pt", "pt-BR", "zh-Hans", "zh-Hant", "ar"}
ASSIGNABLE = {"owner": {"admin", "member", "viewer"}, "admin": {"member", "viewer"}}

@dataclass(frozen=True)
class Principal:            # built from the verified access token, never from tool arguments
    user_id: str
    tenant_id: str
    role: str               # owner | admin | member | viewer

class Users(Protocol):
    def find_by_email(self, tenant_id: str, email: str) -> dict | None: ...
    def create_invite(self, tenant_id: str, email: str, name: str, languages: list[str],
                      role: str, invited_by: str, idempotency_key: str) -> dict: ...

def _ok(data: dict, summary: str) -> dict:
    return {"content": [{"type": "text", "text": summary + "\n" + json.dumps(data, ensure_ascii=False)}],
            "structuredContent": data, "isError": False}

def _err(code: str, message: str, *, field: str | None = None, retryable: bool = False, **extra) -> dict:
    err = {"code": code, "message": message, "retryable": retryable, **({"field": field} if field else {}), **extra}
    return {"content": [{"type": "text", "text": f"Error {code}: {message}"}],
            "structuredContent": {"error": err}, "isError": True}

def _from_field(e: FieldError) -> dict:
    return _err(e.code, e.message, field=e.field)

def invite_user(args: dict, who: Principal, users: Users) -> dict:
    # 1. Authorization first: an unauthorised caller learns nothing about input validity.
    role = args.get("role", "member")
    if role not in ASSIGNABLE.get(who.role, set()):
        allowed = sorted(ASSIGNABLE.get(who.role, set()))
        return _err("FORBIDDEN_ROLE", f"Your role '{who.role}' cannot invite with role '{role}'. "
                    + (f"You may assign: {allowed}." if allowed else "Ask a workspace admin to send the invite."))
    # 2. Normalise + validate every field; report the first failure with a fix hint.
    email, e = normalize_email(args.get("email", ""))
    if e: return _from_field(e)
    name, e = normalize_display_name(args.get("display_name", ""))
    if e: return _from_field(e)
    langs, e = normalize_languages(args.get("preferred_languages") or ["en"], SUPPORTED_LANGUAGES)
    if e: return _from_field(e)
    key = args.get("idempotency_key")
    if not key:
        return _err("IDEMPOTENCY_KEY_REQUIRED", "Pass 'idempotency_key' (any unique string, e.g. a UUID) "
                    "and reuse the same value if you retry this call.", field="idempotency_key")
    # 3. Business rules. Conflicts return what the model needs for its next step.
    existing = users.find_by_email(who.tenant_id, email)
    if existing and existing.get("idempotency_key") != key:
        return _err("ALREADY_MEMBER", f"{email} is already in this workspace (user_id {existing['user_id']}, "
                    f"status {existing['status']}). Do not re-invite; use update_user_role to change the role.",
                    field="email", existing_user_id=existing["user_id"])
    invite = users.create_invite(who.tenant_id, email, name, langs, role, who.user_id, key)
    ui_lang = next(filter(None, (lookup(t, SUPPORTED_LANGUAGES) for t in langs)))
    data = {"invite_id": invite["invite_id"], "email": email, "display_name": name, "role": role,
            "preferred_languages": langs, "email_language": ui_lang, "status": "pending",
            "expires_at": invite["expires_at"]}
    return _ok(data, f"Invited {name} <{email}> as {role}; invitation sent in '{ui_lang}', expires {invite['expires_at']}.")
