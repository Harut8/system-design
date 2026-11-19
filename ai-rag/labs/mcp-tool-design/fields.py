"""Field normalisers for agent-facing tools. Stdlib only; each returns (value, None) or (None, FieldError)."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

@dataclass(frozen=True)
class FieldError:
    field: str
    code: str     # stable, machine-readable: EMAIL_INVALID, NAME_TOO_LONG, ...
    message: str  # written for the model: what is wrong + what a valid value looks like

# --- email ---------------------------------------------------------------
# Deliberately permissive syntax check; deliverability is proven by a verification mail, not a regex.
_LOCAL = re.compile(r"^[^\s@\"(),:;<>\[\]\\]{1,64}$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

def normalize_email(raw: str, field: str = "email") -> tuple[str | None, FieldError | None]:
    s = unicodedata.normalize("NFC", raw.strip())
    if s.count("@") != 1:
        return None, FieldError(field, "EMAIL_INVALID",
            f"'{field}' must contain exactly one '@', e.g. 'ana@example.com'.")
    local, domain = s.split("@")
    try:
        domain = domain.rstrip(".").encode("idna").decode("ascii").lower()  # IDN -> punycode
    except UnicodeError:
        return None, FieldError(field, "EMAIL_INVALID", f"The domain in '{field}' is not a valid hostname.")
    labels = domain.split(".")
    if not _LOCAL.match(local) or len(labels) < 2 or not all(_LABEL.match(l) for l in labels):
        return None, FieldError(field, "EMAIL_INVALID",
            f"'{field}' is not a valid address. Expected local@domain.tld, e.g. 'ana@example.com'.")
    out = f"{local}@{domain}"   # local part kept as typed: only the domain is case-insensitive by standard
    if len(out) > 254:
        return None, FieldError(field, "EMAIL_TOO_LONG", f"'{field}' must be at most 254 characters.")
    return out, None

# --- display name --------------------------------------------------------
_BIDI_CONTROLS = {"‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨", "⁩"}

def normalize_display_name(raw: str, field: str = "display_name", max_chars: int = 100):
    s = unicodedata.normalize("NFC", raw)
    # Drop control (Cc), format (Cf: zero-width, bidi overrides) and unassigned (Cn) code points,
    # but keep ZWJ/ZWNJ, which some scripts (Persian, Hindi) and emoji sequences need.
    s = "".join(ch for ch in s
                if ch in ("‌", "‍")
                or (unicodedata.category(ch) not in ("Cc", "Cf", "Cn") and ch not in _BIDI_CONTROLS))
    s = " ".join(s.split())     # collapse all Unicode whitespace runs to one ASCII space
    if not s:
        return None, FieldError(field, "NAME_EMPTY", f"'{field}' is empty after removing invisible characters.")
    if len(s) > max_chars:      # code points, the same unit JSON Schema maxLength counts
        return None, FieldError(field, "NAME_TOO_LONG",
            f"'{field}' has {len(s)} characters; the maximum is {max_chars}. Shorten it; do not transliterate.")
    return s, None

# --- language tags (BCP 47) ---------------------------------------------
# Covers language[-script][-region][-variant]*; private-use and extensions are rejected on purpose.
_BCP47 = re.compile(r"^([a-z]{2,3})(-[a-z]{4})?(-(?:[a-z]{2}|[0-9]{3}))?((?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*)$", re.I)
_ALIASES = {"iw": "he", "in": "id", "ji": "yi"}  # deprecated ISO 639 codes models still emit

def canonical_language_tag(raw: str) -> str | None:
    m = _BCP47.match(raw.strip().replace("_", "-"))   # models often send POSIX 'pt_BR'
    if not m:
        return None
    lang, script, region, variants = m.groups()
    lang = _ALIASES.get(lang.lower(), lang.lower())
    out = lang
    if script:
        out += "-" + script[1:].title()     # zh-hant -> zh-Hant
    if region:
        out += "-" + region[1:].upper()     # pt-br  -> pt-BR
    if variants:
        out += variants.lower()
    return out

def normalize_languages(raw: list[str], supported: set[str], field: str = "preferred_languages"):
    tags: list[str] = []
    for item in raw:
        tag = canonical_language_tag(item)
        if tag is None:
            return None, FieldError(field, "LANGUAGE_INVALID",
                f"'{item}' is not a BCP 47 language tag. Use tags like 'en', 'pt-BR', 'zh-Hant', 'sr-Latn'.")
        if tag not in tags:
            tags.append(tag)                # keep the caller's preference order, drop duplicates
    if not any(lookup(t, supported) for t in tags):
        return None, FieldError(field, "LANGUAGE_UNSUPPORTED",
            f"None of {tags} is supported. Supported: {sorted(supported)}.")
    return tags, None

def lookup(tag: str, supported: set[str]) -> str | None:
    """RFC 4647 'lookup': strip subtags from the right until one matches (pt-BR -> pt)."""
    parts = tag.split("-")
    while parts:
        cand = "-".join(parts)
        if cand in supported:
            return cand
        parts.pop()
    return None
