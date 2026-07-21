"""Integration tests for gateway.py multi-instance config parsing & wiring.

These tests exercise the JSON config parsing and the create_gateway()
branching between single-instance (legacy) and multi-instance paths.
They do NOT spawn real subprocesses or HTTP servers — the multi-instance
proxy class itself is unit-tested in test_multi_instance.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcp_gateway.gateway import (
    _proxy_config_for_http,
    _proxy_config_for_stdio,
    load_servers,
)


def _write_servers(tmp_path: Path, raw: dict) -> Path:
    p = tmp_path / "servers.json"
    p.write_text(json.dumps(raw))
    return p


# ---------------------------------------------------------------------------
# load_servers: single-instance compat (everything below should keep
# working unchanged, regardless of multi-instance additions)
# ---------------------------------------------------------------------------


class TestLoadServersSingleInstanceCompat:
    def test_legacy_stdio_entry_parses(self, tmp_path: Path) -> None:
        p = _write_servers(tmp_path, {
            "firecrawl": {
                "command": "npx",
                "args": ["-y", "firecrawl-mcp"],
                "transport": "stdio",
                "env_keys": ["FIRECRAWL_API_KEY"],
                "tools": {
                    "scrape": {"name": "fc_scrape", "title": "Scrape"},
                },
            },
        })
        servers = load_servers(p)
        entry = servers["firecrawl"]
        assert entry["transport"] == "stdio"
        assert entry["command"] == "npx"
        assert entry["env_keys"] == ["FIRECRAWL_API_KEY"]
        assert "instances" not in entry

    def test_legacy_http_entry_parses(self, tmp_path: Path) -> None:
        p = _write_servers(tmp_path, {
            "imessage": {
                "transport": "streamable-http",
                "url": "http://127.0.0.1:8001/mcp",
                "tools": {
                    "send_imessage": {"name": "imessage_send"},
                },
            },
        })
        servers = load_servers(p)
        entry = servers["imessage"]
        assert entry["transport"] == "streamable-http"
        assert entry["url"] == "http://127.0.0.1:8001/mcp"
        assert "instances" not in entry

    def test_http_without_url_and_without_instances_errors(
        self, tmp_path: Path
    ) -> None:
        p = _write_servers(tmp_path, {
            "broken": {
                "transport": "streamable-http",
                "tools": {},
            },
        })
        with pytest.raises(ValueError, match="requires 'url'"):
            load_servers(p)


# ---------------------------------------------------------------------------
# load_servers: multi-instance parsing
# ---------------------------------------------------------------------------


class TestLoadServersMultiInstance:
    def test_stdio_multi_instance_parses(self, tmp_path: Path) -> None:
        p = _write_servers(tmp_path, {
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
                "transport": "stdio",
                "param_name": "account",
                "instances": {
                    "personal": {
                        "env": {
                            "GMAIL_OAUTH_PATH": "GMAIL_OAUTH_PATH_PERSONAL",
                        },
                    },
                    "work": {
                        "env": {
                            "GMAIL_OAUTH_PATH": "GMAIL_OAUTH_PATH_WORK",
                        },
                    },
                },
                "tools": {
                    "send_email": {"name": "gmail_send"},
                },
            },
        })
        servers = load_servers(p)
        gmail = servers["gmail"]
        assert gmail["param_name"] == "account"
        assert list(gmail["instances"].keys()) == ["personal", "work"]
        assert gmail["instances"]["personal"]["env_map"] == {
            "GMAIL_OAUTH_PATH": "GMAIL_OAUTH_PATH_PERSONAL",
        }

    def test_stdio_multi_instance_defaults_param_name(
        self, tmp_path: Path
    ) -> None:
        p = _write_servers(tmp_path, {
            "x": {
                "command": "noop",
                "transport": "stdio",
                "instances": {
                    "a": {"env": {}},
                    "b": {"env": {}},
                },
                "tools": {},
            },
        })
        servers = load_servers(p)
        assert servers["x"]["param_name"] == "instance"

    def test_http_multi_instance_parses(self, tmp_path: Path) -> None:
        p = _write_servers(tmp_path, {
            "chrome": {
                "transport": "streamable-http",
                "param_name": "host",
                "instances": {
                    "laptop":  {"url": "http://127.0.0.1:8004/mcp"},
                    "desktop": {"url": "http://127.0.0.1:8014/mcp"},
                },
                "tools": {
                    "click": {"name": "chrome_click"},
                },
            },
        })
        servers = load_servers(p)
        chrome = servers["chrome"]
        assert chrome["param_name"] == "host"
        assert chrome["instances"]["laptop"]["url"] == "http://127.0.0.1:8004/mcp"

    def test_http_multi_instance_missing_url_per_instance_errors(
        self, tmp_path: Path
    ) -> None:
        p = _write_servers(tmp_path, {
            "chrome": {
                "transport": "streamable-http",
                "instances": {
                    "laptop":  {},  # missing url
                },
                "tools": {},
            },
        })
        with pytest.raises(ValueError, match="missing 'url'"):
            load_servers(p)

    def test_empty_instances_block_errors(self, tmp_path: Path) -> None:
        p = _write_servers(tmp_path, {
            "x": {
                "command": "noop",
                "transport": "stdio",
                "instances": {},
                "tools": {},
            },
        })
        with pytest.raises(ValueError, match="empty"):
            load_servers(p)


# ---------------------------------------------------------------------------
# Proxy-config builders (env handling is the trickiest bit)
# ---------------------------------------------------------------------------


class TestProxyConfigForStdio:
    def test_single_instance_uses_top_level_env_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FOO", "foo-val")
        cfg = {
            "command": "npx",
            "args": ["x"],
            "env_keys": ["FOO"],
        }
        out = _proxy_config_for_stdio(cfg)
        assert out["env"] == {"FOO": "foo-val"}

    def test_instance_env_map_renames_host_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GMAIL_OAUTH_PATH_PERSONAL", "/personal.json")
        cfg = {
            "command": "npx",
            "args": [],
            "env_keys": [],
        }
        inst = {
            "env_map": {"GMAIL_OAUTH_PATH": "GMAIL_OAUTH_PATH_PERSONAL"},
            "env_keys": [],
        }
        out = _proxy_config_for_stdio(cfg, instance_overrides=inst)
        # subprocess sees GMAIL_OAUTH_PATH set to the host's
        # GMAIL_OAUTH_PATH_PERSONAL value.
        assert out["env"]["GMAIL_OAUTH_PATH"] == "/personal.json"

    def test_instance_env_keys_pass_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BAR", "bar-val")
        cfg = {
            "command": "noop",
            "args": [],
            "env_keys": [],
        }
        inst = {"env_keys": ["BAR"], "env_map": {}}
        out = _proxy_config_for_stdio(cfg, instance_overrides=inst)
        assert out["env"]["BAR"] == "bar-val"

    def test_top_level_and_instance_env_combine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHARED", "shared-val")
        monkeypatch.setenv("UNIQUE_PERSONAL", "u-val")
        cfg = {"command": "noop", "args": [], "env_keys": ["SHARED"]}
        inst = {
            "env_map": {"UNIQUE": "UNIQUE_PERSONAL"},
            "env_keys": [],
        }
        out = _proxy_config_for_stdio(cfg, instance_overrides=inst)
        assert out["env"]["SHARED"] == "shared-val"
        assert out["env"]["UNIQUE"] == "u-val"


class TestProxyConfigForHttp:
    def test_single_instance_uses_top_level_url(self) -> None:
        cfg = {
            "transport": "streamable-http",
            "url": "http://example/mcp",
            "headers": {"X-Foo": "bar"},
        }
        out = _proxy_config_for_http(cfg)
        assert out["url"] == "http://example/mcp"
        assert out["headers"] == {"X-Foo": "bar"}

    def test_instance_url_overrides_top_level(self) -> None:
        cfg = {
            "transport": "streamable-http",
            "url": None,
            "headers": {},
        }
        inst = {"url": "http://laptop/mcp", "headers": {}}
        out = _proxy_config_for_http(cfg, instance_overrides=inst)
        assert out["url"] == "http://laptop/mcp"
