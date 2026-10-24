import logging

import pytest

from app.stream import signing
from app.stream.cache import ChunkCache
from app.stream.server import RedactSigFilter

KEY = b"x" * 32


def test_sign_verify():
    sig = signing.sign(KEY, 42)
    assert len(sig) == 32 and signing.verify(KEY, 42, sig)
    assert not signing.verify(KEY, 43, sig)
    assert not signing.verify(b"y" * 32, 42, sig)
    assert not signing.verify(KEY, 42, sig.upper())
    assert not signing.verify(KEY, 42, sig[:-1])
    assert not signing.verify(None, 42, sig)
    assert not signing.verify(KEY, 42, None)


def test_compare_is_constant_time(monkeypatch):
    seen = []
    real = signing.hmac.compare_digest
    monkeypatch.setattr(signing.hmac, "compare_digest", lambda a, b: seen.append(1) or real(a, b))
    signing.verify(KEY, 1, "0" * 32)
    assert seen


def test_load_key(tmp_path):
    f = tmp_path / "k"
    f.write_bytes(b"a" * 40 + b"\n")
    assert signing.load_key(str(f)) == b"a" * 40
    assert signing.load_key(str(tmp_path / "missing")) is None
    assert signing.load_key("") is None
    f.write_bytes(b"short")
    with pytest.raises(signing.HmacKeyError):
        signing.load_key(str(f))


def test_redact_path_and_log_filter():
    sig = signing.sign(KEY, 7)
    assert signing.redact_path(f"/s/7/{sig}") == "/s/7/<sig>"
    assert signing.redact_path(f"/s/7/{sig}?x=1") == "/s/7/<sig>?x=1"
    assert signing.redact_path("/api/items") == "/api/items"
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", f"/s/7/{sig}", "1.1", 206), None)
    RedactSigFilter().filter(rec)
    assert sig not in rec.getMessage() and "/s/7/<sig>" in rec.getMessage()


def test_lru_bounded():
    c = ChunkCache(10)
    c.put((1, 0), b"aaaa")
    c.put((1, 1), b"bbbb")
    assert c.get((1, 0)) == b"aaaa"  # now most recent
    c.put((1, 2), b"cccc")  # evicts (1,1)
    assert (1, 1) not in c and (1, 0) in c and c.bytes <= 10
    c.put((2, 0), b"z" * 11)  # larger than cache: ignored
    assert (2, 0) not in c
    c.drop_variant(1)
    assert len(c) == 0 and c.bytes == 0
