"""R2 over S3: SigV4 signing and backend selection.

Why S3 at all: R2's bucket-scoped tokens (Object Read & Write on one bucket)
are refused by the Cloudflare REST object API, even on their own bucket, and
accepted over S3. Measured 2026-11-11 with the mcp-attachments token.
"""

from __future__ import annotations

import datetime as dt

from mcp_gateway.attachments.config import StorageConfig
from mcp_gateway.attachments.store import R2Store, build_store, sigv4_headers


def test_sigv4_matches_aws_published_example():
    # AWS S3 docs, "Example: GET Object" (sig-v4-header-based-auth): the
    # canonical test vector, with its published signature.
    h = sigv4_headers(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        body=b"",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        extra={"Range": "bytes=0-9"},
        region="us-east-1",
        now=dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc),
    )
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert h["x-amz-content-sha256"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def _env(**kw):
    return {"CLOUDFLARE_ACCOUNT_ID": "acct", **kw}


def test_key_pair_selects_s3():
    store = build_store(
        StorageConfig(),
        env=_env(
            MCP_ATTACHMENT_R2_ACCESS_KEY_ID="ak",
            MCP_ATTACHMENT_R2_SECRET_ACCESS_KEY="sk",
            MCP_ATTACHMENT_R2_TOKEN="tok",
        ),
    )
    assert isinstance(store, R2Store) and store.s3
    assert store._url("attachments/x.png") == (
        "https://acct.r2.cloudflarestorage.com/mcp-attachments/attachments/x.png"
    )
    hdrs = store._headers("PUT", "attachments/x.png", b"abc", "image/png")
    assert hdrs["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ak/")
    assert "content-type" in hdrs["Authorization"]  # Content-Type is signed
    assert "Bearer" not in hdrs["Authorization"]


def test_token_only_falls_back_to_rest():
    store = build_store(StorageConfig(), env=_env(MCP_ATTACHMENT_R2_TOKEN="tok"))
    assert isinstance(store, R2Store) and not store.s3
    assert store._url("k").startswith("https://api.cloudflare.com/client/v4/accounts/acct/")
    assert store._headers("GET", "k")["Authorization"] == "Bearer tok"
    assert store.describe() == "r2:mcp-attachments (rest)"


def test_half_a_key_pair_is_not_s3():
    store = build_store(
        StorageConfig(),
        env=_env(MCP_ATTACHMENT_R2_ACCESS_KEY_ID="ak", MCP_ATTACHMENT_R2_TOKEN="tok"),
    )
    assert not store.s3
