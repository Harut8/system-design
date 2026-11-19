# Lab: MCP tool design — `invite_user` (`../../31-mcp-tool-design-best-practices.md`)

The worked example from chapter 31 §10, as runnable code: field normalisers for e-mail, display
names and BCP 47 language tags, and an `invite_user` tool handler that returns MCP
`CallToolResult`-shaped dicts (`content` + `structuredContent` + `isError`).

**Status: rung 2 — implemented.** Stdlib only (Python 3.10+); 20 tests pass.

```bash
pytest -v
```

| File | What it shows |
|---|---|
| `fields.py` | `normalize_email` (IDN → punycode, domain-only lowercasing, 254-char cap), `normalize_display_name` (NFC, strips bidi overrides and zero-width characters, keeps every script), `canonical_language_tag` / `normalize_languages` / `lookup` (BCP 47 canonical case, `pt_BR` → `pt-BR`, deprecated aliases, RFC 4647 lookup fallback) |
| `tool.py` | `invite_user`: authorization before validation, principal from the token (never from arguments), stable error codes with fix hints, idempotency key, conflict that names the next tool to call |
| `test_*.py` | The behaviours the chapter claims, as assertions |

What this deliberately is not: an MCP server. Wire the handler into your SDK's tool registration
(the handler is transport-agnostic on purpose, so it can be unit-tested without a client). The
e-mail check is syntax only — deliverability is proven by the verification mail, not a regex.
