"""Signed capability tokens: sign, verify, tamper, expire, scope, binding."""

from __future__ import annotations

import time

import pytest

from mcp_gateway.attachments.cap import CapError, mint_cap, verify_cap

KEY = b"not-a-real-key-but-32-bytes-long"


def test_roundtrip_carries_key_and_expiry():
    tok = mint_cap("abc.png", signing_key=KEY, ttl_seconds=3600, now=1000.0)
    payload = verify_cap(tok, signing_key=KEY, artifact_key="abc.png", now=1000.0)
    assert payload.key == "abc.png"
    assert payload.exp == 4600
    assert payload.scope == "read"


def test_subject_is_carried_when_given():
    tok = mint_cap("abc.png", signing_key=KEY, ttl_seconds=60, sub="peter@danenberg.ai")
    assert verify_cap(tok, signing_key=KEY).sub == "peter@danenberg.ai"


def test_a_different_key_does_not_verify():
    tok = mint_cap("abc.png", signing_key=KEY, ttl_seconds=60)
    with pytest.raises(CapError, match="bad signature"):
        verify_cap(tok, signing_key=b"some other key", artifact_key="abc.png")


def test_tampering_with_the_payload_breaks_the_signature():
    tok = mint_cap("abc.png", signing_key=KEY, ttl_seconds=60)
    body, sig = tok.split(".")
    forged = mint_cap("other.png", signing_key=b"attacker", ttl_seconds=60).split(".")[0]
    with pytest.raises(CapError, match="bad signature"):
        verify_cap(f"{forged}.{sig}", signing_key=KEY)
    assert verify_cap(f"{body}.{sig}", signing_key=KEY).key == "abc.png"


def test_expiry_fires():
    tok = mint_cap("abc.png", signing_key=KEY, ttl_seconds=10, now=1000.0)
    assert verify_cap(tok, signing_key=KEY, now=1009.0).key == "abc.png"
    with pytest.raises(CapError, match="expired"):
        verify_cap(tok, signing_key=KEY, now=1010.0)
    with pytest.raises(CapError, match="expired"):
        verify_cap(tok, signing_key=KEY, now=time.time() + 10_000)


def test_a_cap_is_bound_to_one_artifact():
    tok = mint_cap("abc.png", signing_key=KEY, ttl_seconds=60)
    with pytest.raises(CapError, match="different artifact"):
        verify_cap(tok, signing_key=KEY, artifact_key="def.png")


def test_scope_is_checked():
    tok = mint_cap("abc.png", signing_key=KEY, ttl_seconds=60, scope="read")
    with pytest.raises(CapError, match="scope"):
        verify_cap(tok, signing_key=KEY, scope="write")


@pytest.mark.parametrize("junk", ["", "nodot", "a.b.c", "!!!.???"])
def test_garbage_is_rejected_not_crashed(junk):
    with pytest.raises(CapError):
        verify_cap(junk, signing_key=KEY)
