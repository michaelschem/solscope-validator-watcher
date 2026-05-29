import argparse
import json
import os
import smtplib
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
from twilio.rest import Client


SFDP_REQUIRED_VERSIONS_API = (
    "https://api.solana.org/api/community/v1/"
    "sfdp_required_versions?cluster=mainnet-beta"
)

AGAVE_RELEASES_API = "https://api.github.com/repos/anza-xyz/agave/releases"

DEFAULT_CONFIG_DIR = Path.home() / ".solscope-validator-watcher"
DEFAULT_CONFIG_PATH = str(DEFAULT_CONFIG_DIR / "config.json")
DEFAULT_LOG_PATH = str(DEFAULT_CONFIG_DIR / "watcher.log")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_version(version: str) -> tuple[int, int, int]:
    cleaned = version.strip().lstrip("v")
    semver = cleaned.split("-")[0]
    major, minor, patch = semver.split(".")
    return int(major), int(minor), int(patch)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def default_rpc_url(cluster: str) -> str:
    return f"https://api.{cluster}.solana.com"


def resolve_rpc_url(validator: dict[str, Any]) -> str:
    custom = validator.get("rpc_url")
    if custom:
        return custom
    return default_rpc_url(validator["cluster"])


def default_watchers() -> dict[str, Any]:
    return {
        "sfdp_version": {
            "enabled": True,
            "api_url": SFDP_REQUIRED_VERSIONS_API,
            "cooldown_minutes": 360,
        },
        "software_outdated": {
            "enabled": True,
            "api_url": AGAVE_RELEASES_API,
            "cooldown_minutes": 360,
        },
        "delinquent": {
            "enabled": True,
            "cooldown_minutes": 1,
        },
    }


def default_notifications() -> dict[str, Any]:
    return {
        "slack_webhooks": [],
        "discord_webhooks": [],
        "webhooks": [],
        "ntfy_topics": [],
        "pagerduty_integration_keys": [],
    }


def normalize_validator(validator: dict[str, Any]) -> dict[str, Any]:
    """Fill in defaults and merge any per-watcher overrides for one validator."""
    result = dict(validator)
    result.setdefault("cluster", "mainnet-beta")
    result.setdefault("name", result.get("identity_pubkey") or "validator")

    watchers = default_watchers()
    for name, cfg in watchers.items():
        prev = (result.get("watchers") or {}).get(name)
        if isinstance(prev, dict):
            cfg.update(prev)
    result["watchers"] = watchers

    notifications = default_notifications()
    notifications.update(result.get("notifications") or {})
    result["notifications"] = notifications
    return result


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a config in the multi-validator schema.

    Accepts the current ``{"validators": [...]}`` schema as well as the legacy
    single-validator schema (top-level ``validator``/``watchers``/``notifications``)
    and migrates it in memory.
    """
    if not config:
        return {"validators": []}

    if "validators" in config:
        validators = config.get("validators") or []
    elif "validator" in config:
        legacy = dict(config["validator"])
        legacy["watchers"] = config.get("watchers", {})
        legacy["notifications"] = config.get("notifications", {})
        validators = [legacy]
    else:
        validators = []

    normalized: dict[str, Any] = {
        "validators": [normalize_validator(v) for v in validators]
    }
    if "state_file" in config:
        normalized["state_file"] = config["state_file"]
    return normalized


def _rpc_call(rpc_url: str, method: str, params: list[Any] | None = None) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    resp = requests.post(rpc_url, json=body, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["result"]


def test_rpc(rpc_url: str) -> tuple[bool, str]:
    """Probe an RPC endpoint with getVersion. Returns (ok, human-readable detail)."""
    try:
        result = _rpc_call(rpc_url, "getVersion")
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        return False, str(exc)
    version = result.get("solana-core", "unknown") if isinstance(result, dict) else "unknown"
    return True, f"Connected (solana-core {version})"


def _get_cluster_node_info(
    rpc_url: str, identity_pubkey: str
) -> tuple[str | None, str | None]:
    """Return ``(version, client_id)`` for the node, or ``(None, None)``.

    ``getClusterNodes`` exposes ``clientId`` (e.g. ``Agave``, ``JitoLabs``,
    ``Frankendancer``), though some nodes report ``Unknown(N)`` when the queried
    RPC doesn't recognize the client's numeric id.
    """
    nodes = _rpc_call(rpc_url, "getClusterNodes")
    for node in nodes:
        if node.get("pubkey") == identity_pubkey:
            return node.get("version"), node.get("clientId")
    return None, None


def _detect_client(client_id: str | None, version_parts: tuple[int, int, int]) -> str:
    """Classify a node as ``"firedancer"`` or ``"agave"``.

    Prefers the explicit ``clientId`` and falls back to the version line
    (Firedancer uses the ``0.x`` range) when ``clientId`` is missing or
    unrecognized (e.g. ``Unknown(11)``).
    """
    name = (client_id or "").lower()
    if "firedancer" in name or "frankendancer" in name:
        return "firedancer"
    if "agave" in name or "jito" in name or "paladin" in name:
        return "agave"
    return "firedancer" if version_parts[0] == 0 else "agave"


def _vote_account_status(rpc_url: str, vote_pubkey: str) -> str:
    """Classify a vote account as ``delinquent``, ``current``, or ``absent``.

    ``absent`` means the endpoint returned neither a current nor a delinquent
    entry for the key. That usually signals a misconfiguration (wrong cluster or
    an RPC that doesn't know this validator) rather than a healthy node, so
    callers should surface it instead of treating it as "not delinquent".
    """
    vote_accounts = _rpc_call(rpc_url, "getVoteAccounts", [{"votePubkey": vote_pubkey}])
    if any(
        item.get("votePubkey") == vote_pubkey
        for item in vote_accounts.get("delinquent", [])
    ):
        return "delinquent"
    if any(
        item.get("votePubkey") == vote_pubkey
        for item in vote_accounts.get("current", [])
    ):
        return "current"
    return "absent"


def _get_sfdp_min_versions(api_url: str) -> tuple[str | None, str | None]:
    """Return ``(agave_min_version, firedancer_min_version)`` from the SFDP API.

    The Solana Foundation publishes a separate required minimum for each client,
    so callers can validate a node against the line it actually runs.
    """
    resp = requests.get(api_url, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data", [])
    if not data:
        raise RuntimeError("No version data from Solana API")
    latest = data[-1]
    return latest.get("agave_min_version"), latest.get("firedancer_min_version")


def _get_latest_agave_version(api_url: str, major: int | None = None) -> str | None:
    """Return the newest stable Agave release as ``x.y.z``.

    Anza no longer encodes the cluster (Mainnet/Testnet) in release names, so we
    select by version instead: the highest stable (non-prerelease) release. When
    ``major`` is given, only releases in that major line are considered, which
    keeps a node from being told it's "outdated" against a newer major it isn't
    expected to run yet. Returns ``None`` if no matching release is found.
    """
    resp = requests.get(api_url, params={"per_page": 100}, timeout=15)
    resp.raise_for_status()
    releases = resp.json()
    if not isinstance(releases, list):
        raise RuntimeError(f"Unexpected response from {api_url}: {releases!r}")

    versions: list[tuple[int, int, int]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        # Skip drafts and pre-releases (betas/rcs); they aren't recommended.
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = (release.get("tag_name") or "").lstrip("v")
        # Skip pre-release tags like "1.2.3-rc.1" that aren't flagged as such.
        if "-" in tag:
            continue
        try:
            parsed = _parse_version(tag)
        except (ValueError, AttributeError):
            continue
        if major is not None and parsed[0] != major:
            continue
        versions.append(parsed)

    if not versions:
        return None

    return "{}.{}.{}".format(*max(versions))


def _send_notifications(
    message: str,
    config: dict[str, Any],
    event: str = "trigger",
    dedup_key: str | None = None,
) -> None:
    """Send a notification to every configured channel.

    ``event`` is ``"trigger"`` for an alert or ``"resolve"`` for a recovery.
    Most channels just deliver ``message`` (which already says "RESOLVED ..."
    on recovery); PagerDuty uses a structured trigger/resolve action keyed by
    ``dedup_key`` so an incident can be opened and later closed automatically.
    """
    notifications = config.get("notifications", {})

    for webhook in notifications.get("slack_webhooks", []):
        requests.post(webhook, json={"text": message}, timeout=10)

    for webhook in notifications.get("discord_webhooks", []):
        requests.post(
            webhook,
            json={"content": message, "username": "SolScope Validator Watcher"},
            timeout=10,
        )

    for webhook in notifications.get("webhooks", []):
        requests.post(webhook, json={"text": message}, timeout=10)

    for topic in notifications.get("ntfy_topics", []):
        requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), timeout=10)

    for integration_key in notifications.get("pagerduty_integration_keys", []):
        if event == "resolve":
            body: dict[str, Any] = {
                "event_action": "resolve",
                "routing_key": integration_key,
            }
        else:
            body = {
                "event_action": "trigger",
                "routing_key": integration_key,
                "payload": {
                    "summary": message,
                    "source": "solscope-validator-watcher",
                    "severity": "error",
                    "custom_details": {"info": message},
                },
            }
        if dedup_key:
            body["dedup_key"] = dedup_key
        requests.post(
            "https://events.pagerduty.com/v2/enqueue",
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/vnd.pagerduty+json;version=2",
            },
            timeout=10,
        )

    twilio_cfg = notifications.get("twilio")
    if twilio_cfg:
        client = Client(twilio_cfg["account_sid"], twilio_cfg["auth_token"])
        for to_phone in twilio_cfg.get("to_phones", []):
            client.messages.create(
                to=to_phone,
                from_=twilio_cfg["from_phone"],
                body=message,
            )

    smtp_cfg = notifications.get("smtp_email")
    if smtp_cfg:
        smtp_host = smtp_cfg["host"]
        smtp_port = int(smtp_cfg.get("port", 587))
        sender = smtp_cfg["from_email"]
        username = smtp_cfg.get("username")
        password = smtp_cfg.get("password")
        use_tls = bool(smtp_cfg.get("use_tls", True))
        for recipient in smtp_cfg.get("to_emails", []):
            email_msg = EmailMessage()
            email_msg["From"] = sender
            email_msg["To"] = recipient
            email_msg["Subject"] = "SolScope Validator Watcher Alert"
            email_msg.set_content(message)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                if use_tls:
                    server.starttls()
                if username and password:
                    server.login(username, password)
                server.send_message(email_msg)


@dataclass
class WatchResult:
    fired: bool
    watcher_name: str
    message: str | None = None
    # Which validator this result is for (filled in by run_once).
    validator: str | None = None
    # Outcome decided by run_once: "ok", "alert" (notified now), "active"
    # (still firing, within cooldown), "resolved" (cleared), "disabled",
    # or "error". Watchers themselves only set "ok"/"alert"/"disabled".
    status: str = "ok"

    def __post_init__(self) -> None:
        if self.fired and self.status == "ok":
            self.status = "alert"


def _cooldown_elapsed(last_sent_iso: str | None, minutes: int) -> bool:
    if not last_sent_iso:
        return True
    last_sent = datetime.fromisoformat(last_sent_iso)
    return (_utc_now() - last_sent) >= timedelta(minutes=minutes)


def _validator_name(validator: dict[str, Any]) -> str:
    """Human-friendly name for alert messages (falls back to identity)."""
    return validator.get("name") or validator.get("identity_pubkey") or "validator"


def _short_pubkey(pubkey: str) -> str:
    """Compact a base58 pubkey to ``ABCD...WXYZ`` for inline references."""
    if not pubkey or len(pubkey) <= 11:
        return pubkey
    return f"{pubkey[:4]}...{pubkey[-4:]}"


def _run_sfdp_version_watcher(
    validator: dict[str, Any], state: dict[str, Any]
) -> WatchResult:
    watcher_name = "sfdp_version"
    watcher_cfg = validator["watchers"][watcher_name]
    if not watcher_cfg.get("enabled", False):
        return WatchResult(False, watcher_name, status="disabled")

    cluster = validator["cluster"]
    identity = validator["identity_pubkey"]
    name = _validator_name(validator)
    short_id = _short_pubkey(identity)
    rpc_url = resolve_rpc_url(validator)
    agave_min, firedancer_min = _get_sfdp_min_versions(
        watcher_cfg.get("api_url", SFDP_REQUIRED_VERSIONS_API)
    )
    validator_version, client_id = _get_cluster_node_info(rpc_url, identity)
    if validator_version is None:
        return WatchResult(
            True,
            watcher_name,
            f"Validator {name} ({short_id}) is not visible in "
            f"getClusterNodes for {cluster}.",
        )

    validator_parts = _parse_version(validator_version)
    client = _detect_client(client_id, validator_parts)
    client_display = client_id or client.capitalize()

    # Compare against the detected client's required minimum. (Agave and
    # Firedancer use different version ranges, so the client must be detected
    # first to avoid comparing across them.) The check is version-only: Jito
    # builds no longer encode "-jito" in their reported version, so a node at or
    # above the minimum passes regardless of the specific Agave/Jito variant.
    required_label = firedancer_min if client == "firedancer" else agave_min
    if not required_label:
        # No minimum published for this client; nothing to enforce.
        return WatchResult(False, watcher_name)
    if validator_parts >= _parse_version(required_label):
        return WatchResult(False, watcher_name)

    message = (
        f"Validator {name} ({short_id}) version check failed. "
        f"Current: {validator_version} ({client_display}). "
        f"Required: {required_label}."
    )
    return WatchResult(True, watcher_name, message)


def _run_delinquent_watcher(
    validator: dict[str, Any], state: dict[str, Any]
) -> WatchResult:
    watcher_name = "delinquent"
    watcher_cfg = validator["watchers"][watcher_name]
    if not watcher_cfg.get("enabled", False):
        return WatchResult(False, watcher_name, status="disabled")

    cluster = validator["cluster"]
    vote_pubkey = validator["vote_pubkey"]
    rpc_url = resolve_rpc_url(validator)
    name = _validator_name(validator)
    short_id = _short_pubkey(validator["identity_pubkey"])

    status = _vote_account_status(rpc_url, vote_pubkey)
    if status == "current":
        return WatchResult(False, watcher_name)
    if status == "delinquent":
        message = (
            f"Validator {name} ({short_id}) appears delinquent on {cluster}. "
            f"Vote account: {vote_pubkey}."
        )
        return WatchResult(True, watcher_name, message)

    # status == "absent": the endpoint doesn't know this vote account. Alert
    # rather than silently passing, since this is almost always a config error.
    message = (
        f"Validator {name} ({short_id}) vote account {vote_pubkey} was not found "
        f"in getVoteAccounts on {cluster} via {rpc_url}. Check the cluster and "
        f"vote pubkey, or set a custom RPC URL that serves this validator."
    )
    return WatchResult(True, watcher_name, message)


def _run_software_outdated_watcher(
    validator: dict[str, Any], state: dict[str, Any]
) -> WatchResult:
    watcher_name = "software_outdated"
    watcher_cfg = validator["watchers"].get(watcher_name, {})
    if not watcher_cfg.get("enabled", False):
        return WatchResult(False, watcher_name, status="disabled")

    cluster = validator["cluster"]
    identity = validator["identity_pubkey"]
    name = _validator_name(validator)
    short_id = _short_pubkey(identity)
    rpc_url = resolve_rpc_url(validator)

    validator_version, client_id = _get_cluster_node_info(rpc_url, identity)
    if validator_version is None:
        return WatchResult(
            True,
            watcher_name,
            f"Validator {name} ({short_id}) is not visible in "
            f"getClusterNodes for {cluster}.",
        )

    validator_parts = _parse_version(validator_version)
    # This watcher tracks Agave GitHub releases; it doesn't apply to Firedancer.
    if _detect_client(client_id, validator_parts) == "firedancer":
        return WatchResult(False, watcher_name)

    latest_version = _get_latest_agave_version(
        watcher_cfg.get("api_url", AGAVE_RELEASES_API),
        major=validator_parts[0],
    )
    # No comparable stable release for this major line: nothing to report.
    if latest_version is None:
        return WatchResult(False, watcher_name)

    latest_parts = _parse_version(latest_version)
    if validator_parts >= latest_parts:
        return WatchResult(False, watcher_name)

    message = (
        f"Validator {name} ({short_id}) has outdated software. "
        f"Current: {validator_version}. "
        f"Latest v{validator_parts[0]}.x release: {latest_version}."
    )
    return WatchResult(True, watcher_name, message)


WATCHER_RUNNERS = {
    "sfdp_version": _run_sfdp_version_watcher,
    "software_outdated": _run_software_outdated_watcher,
    "delinquent": _run_delinquent_watcher,
}

# Human-readable labels for the watcher types.
WATCHER_LABELS = {
    "sfdp_version": "SFDP required version",
    "software_outdated": "Software outdated",
    "delinquent": "Delinquency",
}


def _resolve_message(validator: dict[str, Any], watcher_name: str) -> str:
    """Recovery message sent when a previously-firing watcher clears."""
    name = validator.get("name") or validator.get("identity_pubkey") or "validator"
    label = WATCHER_LABELS.get(watcher_name, watcher_name)
    return f"RESOLVED: {label} check for validator {name} is back to normal."


def run_once(config_path: Path) -> list[WatchResult]:
    raw = _read_json(config_path, {})
    if not raw:
        raise RuntimeError(f"Missing config file: {config_path}")

    config = normalize_config(raw)

    state_path = Path(
        raw.get("state_file", str(config_path.with_suffix(".state.json")))
    ).expanduser()
    state = _read_json(state_path, {})

    results: list[WatchResult] = []
    changed = False
    for validator in config["validators"]:
        vkey = validator.get("name") or validator.get("identity_pubkey") or "validator"
        state_key = validator.get("identity_pubkey") or validator.get("name") or "validator"
        validator_state = state.setdefault(state_key, {})
        for watcher_name, runner in WATCHER_RUNNERS.items():
            try:
                result = runner(validator, validator_state)
            except Exception as exc:  # noqa: BLE001 - keep other watchers running
                # A single flaky check (RPC/API hiccup) must not take down the
                # whole run, or cron would re-fire the same traceback every
                # minute. Log it and move on (don't touch active state, so a
                # transient failure can't masquerade as a recovery).
                print(
                    f"[{vkey}] watcher '{watcher_name}' failed: {exc}",
                    file=sys.stderr,
                )
                results.append(
                    WatchResult(False, watcher_name, validator=vkey, status="error")
                )
                continue

            result.validator = vkey
            results.append(result)
            if result.status == "disabled":
                continue

            wstate = validator_state.setdefault(watcher_name, {})
            prev_active = bool(wstate.get("active", False))
            dedup_key = f"{state_key}:{watcher_name}"
            cooldown = int(
                validator["watchers"].get(watcher_name, {}).get("cooldown_minutes", 360)
            )

            if result.fired:
                # Notify on the first occurrence, or once per cooldown while it
                # persists. Otherwise it's an ongoing alert in its quiet window.
                if (not prev_active) or _cooldown_elapsed(
                    wstate.get("last_sent"), cooldown
                ):
                    if result.message:
                        _send_notifications(
                            result.message,
                            validator,
                            event="trigger",
                            dedup_key=dedup_key,
                        )
                    wstate["last_sent"] = _utc_now().isoformat()
                    result.status = "alert"
                else:
                    result.status = "active"
                wstate["active"] = True
                changed = True
            else:
                # Condition is healthy. If it was firing, send a recovery
                # notification and reset so a re-occurrence alerts immediately.
                if prev_active:
                    _send_notifications(
                        _resolve_message(validator, watcher_name),
                        validator,
                        event="resolve",
                        dedup_key=dedup_key,
                    )
                    result.status = "resolved"
                    wstate["active"] = False
                    wstate.pop("last_sent", None)
                    changed = True

    if changed:
        _write_json(state_path, state)

    return results


def install_cron(config_path: Path, python_bin: str, log_path: Path) -> str:
    cron_cmd = (
        f"* * * * * {python_bin} -m validator_watcher run-once "
        f"--config \"{config_path}\" >> \"{log_path}\" 2>&1"
    )

    Path(log_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    existing = subprocess.run(
        ["crontab", "-l"],
        capture_output=True,
        text=True,
        check=False,
    )
    current_crontab = "" if existing.returncode != 0 else existing.stdout
    lines = [line for line in current_crontab.splitlines() if line.strip()]

    lines = [
        line
        for line in lines
        if f'run-once --config "{config_path}"' not in line
    ]
    lines.append(cron_cmd)

    updated = "\n".join(lines) + "\n"
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    return cron_cmd


def _print_run_summary(results: list[WatchResult]) -> None:
    """Print a per-watcher summary for a ``run-once`` invocation (cron log)."""
    timestamp = _utc_now().isoformat(timespec="seconds")
    alerted = sum(1 for r in results if r.status == "alert")
    resolved = sum(1 for r in results if r.status == "resolved")
    header = (
        f"{timestamp} | {alerted} alert(s) sent, {resolved} resolved "
        f"of {len(results)} checks"
    )
    print(header)

    # Group the per-watcher status lines by validator, preserving order.
    by_validator: dict[str, list[WatchResult]] = {}
    for r in results:
        by_validator.setdefault(r.validator or "validator", []).append(r)

    markers = {
        "alert": "ALERT",
        "active": "still firing",
        "resolved": "RESOLVED",
        "ok": "ok",
        "disabled": "disabled",
        "error": "error",
    }
    for validator, checks in by_validator.items():
        print(f"  {validator}")
        for r in checks:
            label = markers.get(r.status, r.status)
            line = f"    - {r.watcher_name}: {label}"
            if r.message:
                line += f" — {r.message}"
            print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "SolScope standalone validator watcher. "
            "Run with no command to open the full-screen TUI."
        )
    )
    from . import __version__

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to the JSON config file",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run-once", help="Run watchers once (used by cron)")
    run_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)

    cron_parser = sub.add_parser(
        "install-cron", help="Install the one-minute cron job"
    )
    cron_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    cron_parser.add_argument(
        "--python-bin",
        default=os.environ.get("PYTHON_BIN") or sys.executable,
        help="Python binary used by cron (defaults to the current interpreter, "
        "e.g. the active virtualenv)",
    )
    cron_parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_PATH,
        help="Cron log file",
    )

    args = parser.parse_args(argv)
    config_path = Path(args.config)

    if args.command == "run-once":
        results = run_once(config_path)
        _print_run_summary(results)
        return 0

    if args.command == "install-cron":
        cron_cmd = install_cron(config_path, args.python_bin, Path(args.log_file))
        print("Installed cron job:")
        print(cron_cmd)
        return 0

    # No subcommand: launch the full-screen TUI.
    from . import tui

    return tui.run(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
