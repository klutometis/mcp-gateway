"""Match rules and thresholds come from configuration, not from code."""

from __future__ import annotations

import json

import pytest

from mcp_gateway.attachments.config import (
    DEFAULT_INLINE_IMAGE_MAX_BYTES,
    DEFAULT_MIN_BYTES,
    load_config,
    matches,
)

# The four tools the plan names, in the flattened form this gateway
# actually advertises them under.
COVERED = [
    "whatsapp_download_media",
    "linkedin_fetch_attachment",
    "imessage_get_attachment",
    "get_asset_thumbnail",
]
UNCOVERED = ["whatsapp_read_thread", "search_corpus", "view_image", "chrome_screenshot"]


@pytest.mark.parametrize("tool", COVERED)
def test_default_rules_cover_the_four_named_tools(tool):
    cfg = load_config(env={"MCP_ATTACHMENT_OFFLOAD": "1"})
    assert cfg.rule_for(tool) is not None, tool


@pytest.mark.parametrize("tool", UNCOVERED)
def test_default_rules_leave_everything_else_alone(tool):
    cfg = load_config(env={"MCP_ATTACHMENT_OFFLOAD": "1"})
    assert cfg.rule_for(tool) is None, tool


@pytest.mark.parametrize(
    "name,pattern,expected",
    [
        ("whatsapp.download_media", "*.download_media", True),  # canonical shape
        ("whatsapp_download_media", "*.download_media", True),  # gateway's rename
        ("download_media", "*.download_media", True),  # bare
        ("download_media_v2", "*.download_media", False),
        ("gmail_get_attachment", "*.get_attachment", True),
        # Suffix matching is deliberate and has a consequence worth stating:
        # a Gmail rule written as `*.get_attachment` would also catch
        # iMessage's `imessage_get_attachment`. That is the right answer —
        # same verb, same treatment — but it means patterns are about the
        # verb, not about one server.
        ("imessage_get_attachment", "*.get_attachment", True),
        ("anything", "*", True),
    ],
)
def test_pattern_semantics(name, pattern, expected):
    assert matches(name, pattern) is expected


def test_disabled_unless_asked():
    assert load_config(env={}).enabled is False
    assert load_config(env={"MCP_ATTACHMENT_OFFLOAD": "1"}).enabled is True
    assert load_config(env={"MCP_ATTACHMENT_OFFLOAD": "off"}).enabled is False


def test_gmail_is_one_line_of_config(tmp_path):
    """Adding a tool is configuration: no code change, no redeploy of logic."""
    path = tmp_path / "attachments.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "min_bytes": 1234,
                "rules": [
                    {"name": "gmail", "match": ["*.get_attachment"], "max_bytes": 999},
                ],
            }
        )
    )
    cfg = load_config(path, env={})
    assert cfg.enabled is True
    rule = cfg.rule_for("gmail_get_attachment")
    assert rule is not None
    assert rule.min_bytes == 1234  # file-level default inherited
    assert rule.max_bytes == 999  # rule-level override
    assert cfg.rule_for("whatsapp_download_media") is None  # file replaces defaults


def test_env_overrides_thresholds_and_storage(tmp_path):
    cfg = load_config(
        env={
            "MCP_ATTACHMENT_OFFLOAD": "1",
            "MCP_ATTACHMENT_MIN_BYTES": "17",
            "MCP_ATTACHMENT_INLINE_IMAGE_MAX_BYTES": "23",
            "MCP_ATTACHMENT_BACKEND": "file",
            "MCP_ATTACHMENT_DIR": str(tmp_path),
            "MCP_ATTACHMENT_TTL": "60",
            "MCP_ATTACHMENT_BASE_URL": "http://127.0.0.1:3199",
        }
    )
    rule = cfg.rule_for("whatsapp_download_media")
    assert (rule.min_bytes, rule.inline_image_max_bytes) == (17, 23)
    assert cfg.storage.backend == "file" and cfg.storage.directory == str(tmp_path)
    assert cfg.ttl_seconds == 60
    assert cfg.url_for("abc.png", "CAP") == "http://127.0.0.1:3199/artifacts/abc.png?cap=CAP"


def test_defaults_match_the_plan():
    cfg = load_config(env={"MCP_ATTACHMENT_OFFLOAD": "1"})
    rule = cfg.rule_for("linkedin_fetch_attachment")
    assert rule.min_bytes == DEFAULT_MIN_BYTES == 50 * 1024
    assert rule.inline_image_max_bytes == DEFAULT_INLINE_IMAGE_MAX_BYTES
    assert cfg.ttl_seconds == 24 * 3600  # plan: 24h
    assert cfg.view_max_dimension == 1024  # plan: 1024


def test_secrets_are_referenced_by_name_never_by_value():
    cfg = load_config(env={"MCP_ATTACHMENT_OFFLOAD": "1"})
    assert cfg.storage.token_env == "MCP_ATTACHMENT_R2_TOKEN"
    assert cfg.storage.account_id_env == "CLOUDFLARE_ACCOUNT_ID"


def test_signing_key_is_ephemeral_but_present_when_unset():
    a = load_config(env={"MCP_ATTACHMENT_OFFLOAD": "1"})
    b = load_config(env={"MCP_ATTACHMENT_OFFLOAD": "1"})
    assert len(a.signing_key) == 32 and a.signing_key != b.signing_key
    c = load_config(env={"MCP_ATTACHMENT_SIGNING_KEY": "shared"})
    assert c.signing_key == b"shared"
