import hashlib
import hmac

from _pay import verify_rzp_signature


def test_rzp_signature_roundtrip():
    sig = hmac.new(b"secret", b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert verify_rzp_signature("secret", "order_1", "pay_1", sig)
    assert not verify_rzp_signature("secret", "order_1", "pay_1", "deadbeef")
    assert not verify_rzp_signature("other", "order_1", "pay_1", sig)
