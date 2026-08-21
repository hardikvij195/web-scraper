import hashlib
import hmac

from _pay import verify_rzp_signature


def test_rzp_signature_roundtrip():
    sig = hmac.new(b"secret", b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert verify_rzp_signature("secret", "order_1", "pay_1", sig)
    assert not verify_rzp_signature("secret", "order_1", "pay_1", "deadbeef")
    assert not verify_rzp_signature("other", "order_1", "pay_1", sig)


ARGS = dict(key="K", salt="S", txnid="t1", amount="880.00", productinfo="starter_3k",
            firstname="Member", email="m@x.com", udf1="uid-1", udf2="starter_3k")


def test_payu_request_hash_formula():
    from _pay import payu_request_hash
    seq = "K|t1|880.00|starter_3k|Member|m@x.com|uid-1|starter_3k|||||||||S"
    assert payu_request_hash(**ARGS) == hashlib.sha512(seq.encode()).hexdigest()


def test_payu_response_hash_formula():
    from _pay import payu_response_hash
    seq = "S|success||||||||starter_3k|uid-1|m@x.com|Member|starter_3k|880.00|t1|K"
    assert payu_response_hash(status="success", **ARGS) == hashlib.sha512(seq.encode()).hexdigest()
