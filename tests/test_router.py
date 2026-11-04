"""LAN router (hub mode): config, workstation key registry, LAN listener, SDK remote mode."""

from __future__ import annotations

import json
import os
import stat

import pytest

from wind_broker.config import BrokerConfig, load_config


# ----------------------------------------------------------------- config
def test_router_config_defaults_off(home):
    config = load_config(home / "missing.json")
    assert config.router_enabled is False
    assert config.router_host == "0.0.0.0"
    assert config.router_port == 18882
    assert config.router_max_inflight == 2
    assert not {"router_enabled", "router_host", "router_port", "router_max_inflight"} & BrokerConfig.RESTART_KEYS


def test_old_config_without_router_keys_loads(home):
    path = home / "config.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": 18881}))
    config = load_config(path)
    assert config.router_enabled is False and config.router_port == 18882


@pytest.mark.parametrize("changes", [
    {"router_enabled": "yes"},
    {"router_host": ""},
    {"router_port": 70000},
    {"router_port": True},
    {"router_max_inflight": 0},
    {"router_max_inflight": 1.5},
    {"port": 18881, "router_port": 18881},
])
def test_router_config_validation(changes):
    with pytest.raises(ValueError):
        BrokerConfig().updated(changes)


def test_router_port_may_equal_port_when_both_ephemeral():
    assert BrokerConfig().updated({"port": 0, "router_port": 0}).router_port == 0


# --------------------------------------------------------------- registry
def test_registry_add_verify_revoke_and_persist(home):
    from wind_broker.router import ClientRegistry, registry_path

    registry = ClientRegistry()
    key = registry.add("alice-pc")
    assert key.startswith("wbk_") and len(key) > 30
    assert registry.verify(key) == "alice-pc"
    assert registry.verify("wbk_wrong") is None
    assert registry.verify("") is None

    path = registry_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    raw = path.read_text()
    assert key not in raw  # only the digest is stored
    entry = json.loads(raw)["clients"][0]
    assert entry["name"] == "alice-pc" and len(entry["key_sha256"]) == 64 and entry["created_at"]

    reloaded = ClientRegistry()  # survives a daemon restart
    assert reloaded.verify(key) == "alice-pc"
    reloaded.revoke("alice-pc")
    assert reloaded.verify(key) is None
    assert ClientRegistry().verify(key) is None


@pytest.mark.parametrize("name", ["", "-lead", "has space", "x" * 41, "a/b", "über"])
def test_registry_rejects_bad_names(home, name):
    from wind_broker.router import ClientRegistry

    with pytest.raises(ValueError):
        ClientRegistry().add(name)


def test_registry_rejects_duplicates_and_unknown_revoke(home):
    from wind_broker.router import ClientRegistry

    registry = ClientRegistry()
    registry.add("bob")
    with pytest.raises(ValueError):
        registry.add("bob")
    with pytest.raises(KeyError):
        registry.revoke("nobody")


def test_registry_corrupt_file_counts_as_empty(home):
    from wind_broker.router import ClientRegistry, registry_path

    registry_path().write_text("{not json")
    registry = ClientRegistry()
    assert registry.error and registry.clients() == []
    assert registry_path().read_text() == "{not json"  # untouched until an add/revoke
    key = registry.add("carol")
    assert ClientRegistry().verify(key) == "carol"
    assert registry.error is None


def test_registry_inflight_cap_and_stats(home):
    from wind_broker.router import ClientRegistry

    registry = ClientRegistry()
    registry.add("dave")
    assert registry.acquire("dave", 2) and registry.acquire("dave", 2)
    assert not registry.acquire("dave", 2)
    registry.release("dave")
    assert registry.acquire("dave", 2)
    registry.touch("dave", "10.0.0.5")
    (info,) = registry.clients()
    assert info["name"] == "dave" and info["inflight"] == 2 and info["requests"] == 1
    assert info["last_addr"] == "10.0.0.5" and info["last_seen_at"]
    assert "key_sha256" not in info
