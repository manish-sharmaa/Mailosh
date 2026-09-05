from mailosh.security import crypto


def test_roundtrip_and_key_isolation():
    tok = crypto.encrypt("k" * 40, "api-secret")
    assert crypto.decrypt("k" * 40, tok) == "api-secret"
    assert crypto.derive_key("k" * 40, b"sessions") != crypto.derive_key("k" * 40, b"undo")


def test_wrong_key_fails():
    import pytest

    tok = crypto.encrypt("k" * 40, "x")
    with pytest.raises(ValueError):
        crypto.decrypt("j" * 40, tok)
