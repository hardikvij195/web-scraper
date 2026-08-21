import hashlib

from _auth import hash_token, mask


def test_mask_none_and_short():
    assert mask(None) is None
    assert mask("") is None
    assert mask("abc") == "…abc"


def test_mask_long_shows_last4():
    assert mask("sk-1234567890") == "…7890"


def test_hash_token_is_sha256_hex():
    assert hash_token("tok") == hashlib.sha256(b"tok").hexdigest()


def test_new_secret_len_and_uniqueness():
    from _accounts import new_secret
    a, b = new_secret(), new_secret()
    assert a != b and len(a) >= 32
