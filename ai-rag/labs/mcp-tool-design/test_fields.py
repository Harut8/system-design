import pytest
from fields import normalize_email, normalize_display_name, normalize_languages, canonical_language_tag, lookup

@pytest.mark.parametrize("raw,want", [
    ("  Ana.Lopez+news@Example.COM ", "Ana.Lopez+news@example.com"),  # local part kept, domain lowercased
    ("hayk@bücher.example", "hayk@xn--bcher-kva.example"),             # IDN domain -> punycode
])
def test_email_ok(raw, want):
    assert normalize_email(raw) == (want, None)

@pytest.mark.parametrize("raw", ["no-at-sign", "a@@b.com", "a@b", "a b@c.com", "a@-bad.com"])
def test_email_bad(raw):
    value, err = normalize_email(raw)
    assert value is None and err.code == "EMAIL_INVALID"

def test_name_keeps_scripts_and_strips_invisibles():
    assert normalize_display_name("  Արամ‮  Պետրոսյան ")[0] == "Արամ Պետրոսյան"
    assert normalize_display_name("José")[0] == "José"                         # NFD -> NFC
    assert normalize_display_name("​​")[1].code == "NAME_EMPTY"
    assert normalize_display_name("x" * 101)[1].code == "NAME_TOO_LONG"

@pytest.mark.parametrize("raw,want", [("pt_br", "pt-BR"), ("ZH-hant-tw", "zh-Hant-TW"), ("iw", "he"),
                                      ("es-419", "es-419"), ("english", None), ("en-US-x-foo", None)])
def test_tags(raw, want):
    assert canonical_language_tag(raw) == want

def test_languages_order_dedupe_and_fallback():
    tags, err = normalize_languages(["pt_BR", "pt-br", "en"], {"en", "pt"})
    assert tags == ["pt-BR", "en"] and err is None
    assert lookup("pt-BR", {"en", "pt"}) == "pt"
    assert normalize_languages(["ja"], {"en"})[1].code == "LANGUAGE_UNSUPPORTED"
