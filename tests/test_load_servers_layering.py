"""MCP_SERVERS_FILE may name several files, layered like PATH.

The motivating split: server definitions that are safe in a public repo
live in this one, and the ones naming internal hosts and binaries live
in a private repo beside it. Neither file should have to know the other
exists, and neither should have to be a superset of the other.
"""

from __future__ import annotations

import json
import os

import pytest

from mcp_gateway.gateway import load_servers


def write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload))
    return str(p)


BASE = {"alpha": {"command": "a", "tools": {"one": {"name": "alpha_one"}}}}
CORP = {"beta": {"command": "b", "tools": {"two": {"name": "beta_two"}}}}


def test_single_file_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS_FILE", write(tmp_path, "a.json", BASE))
    assert set(load_servers()) == {"alpha"}


def test_two_files_are_unioned(tmp_path, monkeypatch):
    joined = os.pathsep.join(
        [write(tmp_path, "a.json", BASE), write(tmp_path, "b.json", CORP)]
    )
    monkeypatch.setenv("MCP_SERVERS_FILE", joined)
    assert set(load_servers()) == {"alpha", "beta"}


def test_later_file_replaces_a_server_of_the_same_name(tmp_path, monkeypatch):
    override = {"alpha": {"command": "override", "tools": {}}}
    joined = os.pathsep.join(
        [write(tmp_path, "a.json", BASE), write(tmp_path, "o.json", override)]
    )
    monkeypatch.setenv("MCP_SERVERS_FILE", joined)
    assert load_servers()["alpha"]["command"] == "override"


def test_explicit_path_argument_beats_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS_FILE", write(tmp_path, "env.json", CORP))
    assert set(load_servers(write(tmp_path, "arg.json", BASE))) == {"alpha"}


def test_empty_segments_are_ignored(tmp_path, monkeypatch):
    # A trailing separator is what you get from naive shell concatenation
    # of an unset variable; it should not be a crash.
    monkeypatch.setenv(
        "MCP_SERVERS_FILE", write(tmp_path, "a.json", BASE) + os.pathsep
    )
    assert set(load_servers()) == {"alpha"}


def test_missing_file_in_the_list_is_loud(tmp_path, monkeypatch):
    # Silently skipping would mean a typo costs you a whole stack of
    # tools with no indication why they vanished.
    monkeypatch.setenv(
        "MCP_SERVERS_FILE",
        os.pathsep.join([write(tmp_path, "a.json", BASE), str(tmp_path / "nope.json")]),
    )
    with pytest.raises(FileNotFoundError):
        load_servers()


def test_underscore_keys_are_comments_not_servers(tmp_path, monkeypatch):
    # JSON has no comments, and a curated file most needs to explain the
    # tools it leaves out.
    payload = dict(BASE)
    payload["_comment"] = ["why this file omits things", "second line"]
    monkeypatch.setenv("MCP_SERVERS_FILE", write(tmp_path, "a.json", payload))
    assert set(load_servers()) == {"alpha"}


def test_unset_env_still_raises(monkeypatch):
    monkeypatch.delenv("MCP_SERVERS_FILE", raising=False)
    with pytest.raises(RuntimeError, match="MCP_SERVERS_FILE"):
        load_servers()
