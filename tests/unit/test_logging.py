from __future__ import annotations

from app.observability.logging import REDACTED, scrub, scrub_processor


def test_sensitive_keys_redacted_case_insensitive() -> None:
    out = scrub_processor(None, "info", {"Authorization": "Bearer abc", "X-API-Key": "k", "ok": 1})
    assert out == {"Authorization": REDACTED, "X-API-Key": REDACTED, "ok": 1}


def test_nested_headers_redacted() -> None:
    out = scrub(
        {"headers": {"x-api-key": "sk_live_123", "accept": "json"}, "list": [{"password": "p"}]}
    )
    assert out == {
        "headers": {"x-api-key": REDACTED, "accept": "json"},
        "list": [{"password": REDACTED}],
    }


def test_patterns_in_free_text() -> None:
    text = "key=sk_abcdefghijklmnopqrstu iban GB82WEST12345698765432 auth Bearer eyJhbGciOi.x.y"
    out = scrub(text)
    assert "sk_abcdefghijklmnopqrstu" not in out
    assert "GB82WEST12345698765432" not in out
    assert "eyJhbGciOi" not in out
    assert out.count(REDACTED) == 3


def test_ordinary_text_untouched() -> None:
    assert scrub("invoice INV-2026-001 total 120.50") == "invoice INV-2026-001 total 120.50"
