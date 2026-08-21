import hashlib
import hmac

from _webhooks import sign


def test_sign_matches_manual_hmac():
    body = b'{"a":1}'
    assert sign("sec", body) == hmac.new(b"sec", body, hashlib.sha256).hexdigest()
