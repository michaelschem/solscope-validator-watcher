"""Focused tests for the watcher logic that actually decides whether to alert.

Network access (Solana RPC, SFDP API, GitHub releases) is always mocked so the
suite is fast and deterministic; the goal is to lock down the decision logic and
the trigger/resolve state machine, not to exercise real endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from validator_watcher import app


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def test_parse_version_strips_prefix_and_prerelease():
    assert app._parse_version("v2.1.3") == (2, 1, 3)
    assert app._parse_version("2.1.3-jito") == (2, 1, 3)
    assert app._parse_version("0.909.0-rc.40001") == (0, 909, 0)


@pytest.mark.parametrize(
    "client_id, version, expected",
    [
        ("Frankendancer", (0, 909, 0), "firedancer"),
        ("Firedancer", (0, 1, 0), "firedancer"),
        ("JitoLabs", (2, 1, 0), "agave"),
        ("Agave", (2, 1, 0), "agave"),
        # Unknown clientId falls back to the version line (0.x == Firedancer).
        (None, (0, 909, 0), "firedancer"),
        ("Unknown(11)", (2, 0, 0), "agave"),
    ],
)
def test_detect_client(client_id, version, expected):
    assert app._detect_client(client_id, version) == expected


def test_short_pubkey():
    assert app._short_pubkey("FjvEcsfidtQc5vSRNyxxtpXPtEHq5Gg17MEKcDWhBAXT") == "FjvE...BAXT"
    # Short strings are returned unchanged.
    assert app._short_pubkey("abc") == "abc"


def test_validator_name_falls_back_to_identity():
    assert app._validator_name({"name": "Hyper"}) == "Hyper"
    assert app._validator_name({"identity_pubkey": "ID123"}) == "ID123"
    assert app._validator_name({}) == "validator"


def test_cooldown_elapsed():
    assert app._cooldown_elapsed(None, 60) is True
    now = app._utc_now().isoformat()
    assert app._cooldown_elapsed(now, 60) is False
    old = (app._utc_now() - app.timedelta(minutes=120)).isoformat()
    assert app._cooldown_elapsed(old, 60) is True


# --------------------------------------------------------------------------- #
# Config normalization
# --------------------------------------------------------------------------- #
def test_normalize_validator_fills_defaults_and_keeps_overrides():
    v = app.normalize_validator(
        {
            "identity_pubkey": "ID",
            "watchers": {"delinquent": {"cooldown_minutes": 5}},
        }
    )
    assert v["cluster"] == "mainnet-beta"
    assert v["name"] == "ID"  # falls back to identity
    # default cooldowns present, override preserved
    assert v["watchers"]["sfdp_version"]["cooldown_minutes"] == 360
    assert v["watchers"]["delinquent"]["cooldown_minutes"] == 5
    assert v["watchers"]["delinquent"]["enabled"] is True


def test_delinquent_cooldown_default_is_one_minute():
    v = app.normalize_validator({"identity_pubkey": "ID"})
    assert v["watchers"]["delinquent"]["cooldown_minutes"] == 1


def test_normalize_config_migrates_legacy_single_validator():
    cfg = app.normalize_config(
        {
            "validator": {"identity_pubkey": "ID", "cluster": "testnet"},
            "watchers": {"delinquent": {"enabled": False}},
        }
    )
    assert len(cfg["validators"]) == 1
    only = cfg["validators"][0]
    assert only["cluster"] == "testnet"
    assert only["watchers"]["delinquent"]["enabled"] is False


# --------------------------------------------------------------------------- #
# Vote-account status (the delinquency primitive)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "rpc_result, expected",
    [
        ({"current": [{"votePubkey": "VOTE"}], "delinquent": []}, "current"),
        ({"current": [], "delinquent": [{"votePubkey": "VOTE"}]}, "delinquent"),
        ({"current": [], "delinquent": []}, "absent"),  # nothing returned
    ],
)
def test_vote_account_status(monkeypatch, rpc_result, expected):
    monkeypatch.setattr(app, "_rpc_call", lambda url, method, params=None: rpc_result)
    assert app._vote_account_status("http://rpc", "VOTE") == expected


# --------------------------------------------------------------------------- #
# Delinquency watcher
# --------------------------------------------------------------------------- #
def _validator(**overrides):
    base = {
        "name": "Hyper",
        "cluster": "testnet",
        "identity_pubkey": "FjvEcsfidtQc5vSRNyxxtpXPtEHq5Gg17MEKcDWhBAXT",
        "vote_pubkey": "VOTE",
    }
    base.update(overrides)
    return app.normalize_validator(base)


def test_delinquent_disabled_short_circuits():
    v = _validator(watchers={"delinquent": {"enabled": False}})
    res = app._run_delinquent_watcher(v, {})
    assert res.fired is False
    assert res.status == "disabled"


def test_delinquent_current_is_ok(monkeypatch):
    monkeypatch.setattr(app, "_vote_account_status", lambda u, v: "current")
    res = app._run_delinquent_watcher(_validator(), {})
    assert res.fired is False
    assert res.status == "ok"


def test_delinquent_fires_with_name_and_short_id(monkeypatch):
    monkeypatch.setattr(app, "_vote_account_status", lambda u, v: "delinquent")
    res = app._run_delinquent_watcher(_validator(), {})
    assert res.fired is True
    assert "Hyper" in res.message
    assert "FjvE...BAXT" in res.message  # truncated identity, not full pubkey


def test_delinquent_absent_alerts_instead_of_passing(monkeypatch):
    monkeypatch.setattr(app, "_vote_account_status", lambda u, v: "absent")
    res = app._run_delinquent_watcher(_validator(), {})
    assert res.fired is True
    assert "not found" in res.message


# --------------------------------------------------------------------------- #
# SFDP required-version watcher (client-aware, epoch-deadline-aware comparison)
# --------------------------------------------------------------------------- #
def _sfdp_requirements(*entries):
    """Build the per-epoch requirement list the SFDP API helper returns."""
    return [
        {"epoch": epoch, "agave": agave, "firedancer": firedancer}
        for epoch, agave, firedancer in entries
    ]


def _epoch_info(epoch, slot_index=0, slots_in_epoch=432_000):
    return {"epoch": epoch, "slotIndex": slot_index, "slotsInEpoch": slots_in_epoch}


def test_sfdp_not_visible_alerts(monkeypatch):
    monkeypatch.setattr(
        app,
        "_get_sfdp_requirements",
        lambda url: _sfdp_requirements((800, "2.1.0", "0.900.0")),
    )
    monkeypatch.setattr(app, "_get_cluster_node_info", lambda url, ident: (None, None))
    res = app._run_sfdp_version_watcher(_validator(), {})
    assert res.fired is True
    assert "not visible" in res.message


def test_sfdp_agave_below_minimum_is_overdue(monkeypatch):
    monkeypatch.setattr(
        app,
        "_get_sfdp_requirements",
        lambda url: _sfdp_requirements((800, "2.1.0", "0.900.0")),
    )
    monkeypatch.setattr(app, "_get_cluster_node_info", lambda url, ident: ("2.0.0", "Agave"))
    monkeypatch.setattr(app, "_get_epoch_info", lambda url: _epoch_info(810))
    res = app._run_sfdp_version_watcher(_validator(), {})
    assert res.fired is True
    assert res.message.startswith("SFDP:")  # distinguishable from software_outdated
    assert "SFDP requires 2.1.0" in res.message
    assert "overdue" in res.message
    assert res.detail == "2.0.0 != 2.1.0"  # shown in the dashboard grid


def test_sfdp_future_deadline_reports_hours_left(monkeypatch):
    # Node meets today's minimum but not the one starting at epoch 812. With
    # half of epoch 810 left plus one whole epoch (432k slots each, 0.4s/slot),
    # the estimate is (216000 + 432000) * 0.4s = 3 days = 72 hours.
    monkeypatch.setattr(
        app,
        "_get_sfdp_requirements",
        lambda url: _sfdp_requirements((800, "2.0.0", None), (812, "2.2.0", None)),
    )
    monkeypatch.setattr(app, "_get_cluster_node_info", lambda url, ident: ("2.1.0", "Agave"))
    monkeypatch.setattr(
        app, "_get_epoch_info", lambda url: _epoch_info(810, slot_index=216_000)
    )
    res = app._run_sfdp_version_watcher(_validator(), {})
    assert res.fired is True
    assert "SFDP will require 2.2.0 starting epoch 812" in res.message
    assert "about 72 hours left to update" in res.message
    assert res.detail == "2.1.0 != 2.2.0"


def test_sfdp_agave_at_or_above_minimum_ok(monkeypatch):
    monkeypatch.setattr(
        app,
        "_get_sfdp_requirements",
        lambda url: _sfdp_requirements((800, "2.1.0", "0.900.0")),
    )
    monkeypatch.setattr(app, "_get_cluster_node_info", lambda url, ident: ("2.2.0", "Agave"))
    res = app._run_sfdp_version_watcher(_validator(), {})
    assert res.fired is False
    assert res.detail == "2.2.0"  # version shown once when healthy


def test_sfdp_firedancer_compared_against_firedancer_minimum(monkeypatch):
    # Agave min would flag a 0.x node, but a Firedancer node must be compared to
    # the firedancer minimum -> at/above it is OK.
    monkeypatch.setattr(
        app,
        "_get_sfdp_requirements",
        lambda url: _sfdp_requirements((800, "2.1.0", "0.908.0")),
    )
    monkeypatch.setattr(
        app, "_get_cluster_node_info", lambda url, ident: ("0.909.0", "Frankendancer")
    )
    res = app._run_sfdp_version_watcher(_validator(), {})
    assert res.fired is False


def test_format_hours_left():
    assert app._format_hours_left(3.0) == "72 hours"
    assert app._format_hours_left(0.5) == "12 hours"
    assert app._format_hours_left(0.02) == "1 hour"


# --------------------------------------------------------------------------- #
# Software-outdated watcher (Agave GitHub releases)
# --------------------------------------------------------------------------- #
def test_software_outdated_skips_firedancer(monkeypatch):
    monkeypatch.setattr(
        app, "_get_cluster_node_info", lambda url, ident: ("0.909.0", "Frankendancer")
    )
    # Should never consult GitHub for a Firedancer node.
    monkeypatch.setattr(
        app,
        "_get_latest_agave_version",
        lambda *a, **k: pytest.fail("should not check releases for Firedancer"),
    )
    res = app._run_software_outdated_watcher(_validator(), {})
    assert res.fired is False


def test_software_outdated_alerts_when_behind(monkeypatch):
    monkeypatch.setattr(app, "_get_cluster_node_info", lambda url, ident: ("2.0.0", "Agave"))
    monkeypatch.setattr(app, "_get_latest_agave_version", lambda url, major=None: "2.1.0")
    res = app._run_software_outdated_watcher(_validator(), {})
    assert res.fired is True
    assert "2.1.0" in res.message
    assert res.detail == "2.0.0 != 2.1.0"


def test_software_outdated_ok_when_current(monkeypatch):
    monkeypatch.setattr(app, "_get_cluster_node_info", lambda url, ident: ("2.1.0", "Agave"))
    monkeypatch.setattr(app, "_get_latest_agave_version", lambda url, major=None: "2.1.0")
    res = app._run_software_outdated_watcher(_validator(), {})
    assert res.fired is False
    assert res.detail == "2.1.0"


# --------------------------------------------------------------------------- #
# run_once: trigger -> active (cooldown) -> resolve state machine
# --------------------------------------------------------------------------- #
def test_run_once_trigger_active_resolve(tmp_path: Path, monkeypatch):
    sent: list[tuple[str, str]] = []

    def fake_send(message, config, event="trigger", dedup_key=None):
        sent.append((event, dedup_key))

    monkeypatch.setattr(app, "_send_notifications", fake_send)

    fired = {"value": True}

    def fake_runner(validator, state):
        return app.WatchResult(
            fired["value"], "delinquent", "boom" if fired["value"] else ""
        )

    monkeypatch.setattr(app, "WATCHER_RUNNERS", {"delinquent": fake_runner})

    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    config_path.write_text(
        json.dumps(
            {
                "state_file": str(state_path),
                "validators": [
                    {
                        "name": "V1",
                        "cluster": "mainnet-beta",
                        "identity_pubkey": "ID",
                        "vote_pubkey": "VOTE",
                        # large cooldown so re-firing within the test stays quiet
                        "watchers": {"delinquent": {"enabled": True, "cooldown_minutes": 60}},
                    }
                ],
            }
        )
    )

    # Run 1: condition fires for the first time -> one trigger notification.
    app.run_once(config_path)
    assert sent == [("trigger", "ID:delinquent")]
    state = json.loads(state_path.read_text())
    assert state["ID"]["delinquent"]["active"] is True

    # Run 2: still firing but within cooldown -> no new notification.
    app.run_once(config_path)
    assert sent == [("trigger", "ID:delinquent")]

    # Run 3: condition clears -> exactly one resolve notification.
    fired["value"] = False
    results = app.run_once(config_path)
    assert sent == [("trigger", "ID:delinquent"), ("resolve", "ID:delinquent")]
    state = json.loads(state_path.read_text())
    assert state["ID"]["delinquent"]["active"] is False
    assert any(r.status == "resolved" for r in results)


def test_run_once_isolates_watcher_failures(tmp_path: Path, monkeypatch):
    """A throwing watcher must not abort the run or masquerade as a recovery."""
    monkeypatch.setattr(app, "_send_notifications", lambda *a, **k: None)

    def boom(validator, state):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(app, "WATCHER_RUNNERS", {"delinquent": boom})

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "state_file": str(tmp_path / "state.json"),
                "validators": [
                    {"name": "V1", "identity_pubkey": "ID", "vote_pubkey": "VOTE"}
                ],
            }
        )
    )

    results = app.run_once(config_path)
    assert len(results) == 1
    assert results[0].status == "error"
    assert results[0].fired is False
