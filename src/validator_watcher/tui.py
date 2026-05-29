"""Full-screen Textual TUI for solscope-validator-watcher.

This is the package's primary (human) entrypoint. It shows the configured
validators and how each is set up to alert, lets you add/edit/delete validators,
test notification channels, and install the cron job that runs the watchers.

The non-interactive ``run-once`` command (used by cron) lives in ``app.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    Switch,
)

from . import __version__
from .app import (
    DEFAULT_LOG_PATH,
    WATCHER_LABELS,
    WATCHER_RUNNERS,
    _detect_client,
    _get_cluster_node_info,
    _parse_version,
    _read_json,
    _send_notifications,
    _write_json,
    default_rpc_url,
    install_cron,
    normalize_config,
    normalize_validator,
    resolve_rpc_url,
    test_rpc,
)

_CLUSTERS = ["mainnet-beta", "testnet"]

# Short column headers for the dashboard grid (one per watcher).
_WATCHER_COLUMNS = {
    "sfdp_version": "SFDP ver",
    "software_outdated": "Outdated",
    "delinquent": "Delinquent",
}

# Placeholder shown in a grid cell while its check is still running.
_CELL_CHECKING = "[dim]…[/dim]"


def _status_cell(result: Any) -> str:
    """Render a watcher result as a colored grid cell."""
    if result.status == "disabled":
        return "[dim]— off[/dim]"
    if result.fired:
        return "[bold red]✗ alarm[/bold red]"
    return "[bold green]✓ ok[/bold green]"

# notifications key -> human label, for list-style channels.
_LIST_CHANNELS = [
    ("slack_webhooks", "Slack webhooks"),
    ("discord_webhooks", "Discord webhooks"),
    ("webhooks", "Generic webhooks"),
    ("ntfy_topics", "ntfy topics"),
    ("pagerduty_integration_keys", "PagerDuty integration keys"),
]


class FormScroll(VerticalScroll, can_focus=False):
    """A scroll container that doesn't capture arrow keys.

    The default ScrollableContainer binds Up/Down/etc. to scrolling, which would
    shadow the editor's field-to-field focus navigation. Clearing BINDINGS lets
    those keys bubble up to the screen (the focused field still scrolls into view
    automatically). Inner widgets like Select keep their own key handling.
    """

    BINDINGS: list = []


def _csv_to_list(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _list_to_csv(values: list[str] | None) -> str:
    return ", ".join(values or [])


def _channel_summary(notifications: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, _label in _LIST_CHANNELS:
        if notifications.get(key):
            parts.append(key.split("_")[0].capitalize())
    twilio = notifications.get("twilio") or {}
    if twilio.get("account_sid"):
        parts.append("Twilio")
    smtp = notifications.get("smtp_email") or {}
    if smtp.get("host"):
        parts.append("Email")
    return ", ".join(parts) if parts else "no channels"


def _build_test_message(identity: str) -> str:
    return (
        f"\u2705 SolScope Validator Watcher test alert for {identity or 'validator'}. "
        "If you received this, notifications are working."
    )


def _send_test(notifications: dict[str, Any], identity: str) -> list[tuple[str, bool, str]]:
    """Send a test through each configured channel, isolating failures."""
    message = _build_test_message(identity)
    results: list[tuple[str, bool, str]] = []

    channels: list[tuple[str, str]] = [*_LIST_CHANNELS, ("twilio", "Twilio SMS"), ("smtp_email", "SMTP email")]
    for key, label in channels:
        value = notifications.get(key)
        if not value:
            continue
        if key == "twilio" and not value.get("account_sid"):
            continue
        if key == "smtp_email" and not value.get("host"):
            continue
        try:
            _send_notifications(message, {"notifications": {key: value}})
            results.append((label, True, ""))
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
            results.append((label, False, str(exc)))
    return results


class ValidatorScreen(Screen):
    """Add or edit a single validator and its watchers + channels."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "save", "Save"),
        ("ctrl+t", "test_notif", "Test notifications"),
        ("ctrl+d", "delete", "Delete"),
        Binding("down", "focus_next", "Next field", show=False, priority=True),
        Binding("up", "focus_previous", "Prev field", show=False, priority=True),
    ]

    def __init__(self, validator: dict[str, Any], is_new: bool) -> None:
        super().__init__()
        self.validator = normalize_validator(validator)
        self.is_new = is_new

    def compose(self) -> ComposeResult:
        yield Header()
        title = "Add validator" if self.is_new else "Edit validator"
        with FormScroll(id="form"):
            yield Static(f"[b]{title}[/b]", classes="section")

            yield Label("Name")
            yield Input(value=self.validator.get("name", ""), id="name")

            yield Label("Cluster")
            yield Select(
                [(c, c) for c in _CLUSTERS],
                value=self.validator.get("cluster", "mainnet-beta"),
                allow_blank=False,
                id="cluster",
            )

            yield Label("Custom RPC URL (blank = public endpoint)")
            yield Input(value=self.validator.get("rpc_url", ""), id="rpc_url")

            yield Label("Identity pubkey")
            yield Input(value=self.validator.get("identity_pubkey", ""), id="identity")

            yield Label("Vote pubkey")
            yield Input(value=self.validator.get("vote_pubkey", ""), id="vote")

            with Horizontal(classes="row"):
                yield Button("Test RPC", id="test-rpc", variant="primary")
                yield Static("", id="rpc-status", classes="status")

            yield Static("[b]Watchers[/b]", classes="section")
            for name, label in WATCHER_LABELS.items():
                wcfg = self.validator["watchers"].get(name, {})
                with Horizontal(classes="watch-row"):
                    yield Switch(
                        value=bool(wcfg.get("enabled", True)),
                        id=f"watch-{name}-enabled",
                    )
                    yield Label(f"{label}  ", classes="watch-label")
                    yield Label("cooldown (min):", classes="cooldown-label")
                    yield Input(
                        value=str(wcfg.get("cooldown_minutes", 60)),
                        id=f"watch-{name}-cooldown",
                        type="integer",
                        classes="cooldown-input",
                    )

            yield Static("[b]Notification channels[/b]", classes="section")
            notifications = self.validator.get("notifications", {})
            for key, label in _LIST_CHANNELS:
                yield Label(f"{label} (comma-separated)")
                yield Input(value=_list_to_csv(notifications.get(key)), id=f"chan-{key}")

            twilio = notifications.get("twilio") or {}
            yield Static("[b]Twilio SMS[/b] (your own account)", classes="subsection")
            yield Label("Account SID")
            yield Input(value=twilio.get("account_sid", ""), id="twilio-sid")
            yield Label("Auth token")
            yield Input(value=twilio.get("auth_token", ""), password=True, id="twilio-token")
            yield Label("From phone")
            yield Input(value=twilio.get("from_phone", ""), id="twilio-from")
            yield Label("To phones (comma-separated)")
            yield Input(value=_list_to_csv(twilio.get("to_phones")), id="twilio-to")

            smtp = notifications.get("smtp_email") or {}
            yield Static("[b]SMTP email[/b]", classes="subsection")
            yield Label("Host")
            yield Input(value=smtp.get("host", ""), id="smtp-host")
            yield Label("Port")
            yield Input(value=str(smtp.get("port", 587)), id="smtp-port", type="integer")
            yield Label("Username")
            yield Input(value=smtp.get("username", ""), id="smtp-user")
            yield Label("Password")
            yield Input(value=smtp.get("password", ""), password=True, id="smtp-pass")
            yield Label("From email")
            yield Input(value=smtp.get("from_email", ""), id="smtp-from")
            yield Label("To emails (comma-separated)")
            yield Input(value=_list_to_csv(smtp.get("to_emails")), id="smtp-to")
            with Horizontal(classes="watch-row"):
                yield Switch(value=bool(smtp.get("use_tls", True)), id="smtp-tls")
                yield Label("Use STARTTLS", classes="watch-label")

            with Horizontal(classes="row buttons"):
                yield Button("Save", id="save", variant="success")
                yield Button("Test notifications", id="test-notif", variant="primary")
                if not self.is_new:
                    yield Button("Delete", id="delete", variant="error")
                yield Button("Cancel", id="cancel", variant="default")
        yield Footer()

    def _collect(self) -> dict[str, Any]:
        validator = dict(self.validator)
        validator["name"] = self.query_one("#name", Input).value.strip() or "validator"
        validator["cluster"] = self.query_one("#cluster", Select).value
        rpc = self.query_one("#rpc_url", Input).value.strip()
        if rpc:
            validator["rpc_url"] = rpc
        else:
            validator.pop("rpc_url", None)
        validator["identity_pubkey"] = self.query_one("#identity", Input).value.strip()
        validator["vote_pubkey"] = self.query_one("#vote", Input).value.strip()

        watchers: dict[str, Any] = {}
        for name in WATCHER_LABELS:
            prev = dict(self.validator["watchers"].get(name, {}))
            prev["enabled"] = self.query_one(f"#watch-{name}-enabled", Switch).value
            cooldown = self.query_one(f"#watch-{name}-cooldown", Input).value.strip()
            if cooldown.isdigit():
                prev["cooldown_minutes"] = int(cooldown)
            watchers[name] = prev
        validator["watchers"] = watchers

        validator["notifications"] = self._collect_notifications()
        return validator

    def _collect_notifications(self) -> dict[str, Any]:
        notifications: dict[str, Any] = {}
        for key, _label in _LIST_CHANNELS:
            notifications[key] = _csv_to_list(self.query_one(f"#chan-{key}", Input).value)

        sid = self.query_one("#twilio-sid", Input).value.strip()
        if sid:
            notifications["twilio"] = {
                "account_sid": sid,
                "auth_token": self.query_one("#twilio-token", Input).value.strip(),
                "from_phone": self.query_one("#twilio-from", Input).value.strip(),
                "to_phones": _csv_to_list(self.query_one("#twilio-to", Input).value),
            }

        host = self.query_one("#smtp-host", Input).value.strip()
        if host:
            port = self.query_one("#smtp-port", Input).value.strip()
            notifications["smtp_email"] = {
                "host": host,
                "port": int(port) if port.isdigit() else 587,
                "username": self.query_one("#smtp-user", Input).value.strip(),
                "password": self.query_one("#smtp-pass", Input).value,
                "from_email": self.query_one("#smtp-from", Input).value.strip(),
                "to_emails": _csv_to_list(self.query_one("#smtp-to", Input).value),
                "use_tls": self.query_one("#smtp-tls", Switch).value,
            }
        return notifications

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        validator = self._collect()
        if not validator["identity_pubkey"] or not validator["vote_pubkey"]:
            self.notify(
                "Identity and vote pubkeys are required.", severity="error"
            )
            return
        self.dismiss(("save", validator))

    def action_test_notif(self) -> None:
        self._on_test_notif()

    def action_delete(self) -> None:
        if not self.is_new:
            self.dismiss(("delete", None))

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _on_save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#delete")
    def _on_delete(self) -> None:
        self.dismiss(("delete", None))

    @on(Button.Pressed, "#test-rpc")
    def _on_test_rpc(self) -> None:
        cluster = self.query_one("#cluster", Select).value
        rpc = self.query_one("#rpc_url", Input).value.strip()
        url = rpc or default_rpc_url(cluster)
        identity = self.query_one("#identity", Input).value.strip()
        status = self.query_one("#rpc-status", Static)
        status.update("Testing ...")
        self._run_rpc_test(url, identity, status)

    @work(thread=True)
    def _run_rpc_test(self, url: str, identity: str, status: Static) -> None:
        ok, detail = test_rpc(url)
        marker = "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"
        # When an identity is set, also report the detected client + version.
        if ok and identity:
            try:
                version, client_id = _get_cluster_node_info(url, identity)
                if version is None:
                    detail += " | node not found in getClusterNodes"
                else:
                    client = _detect_client(client_id, _parse_version(version))
                    label = client_id or client.capitalize()
                    detail += f" | {label} {version}"
            except Exception as exc:  # noqa: BLE001
                detail += f" | client lookup failed: {exc}"
        self.app.call_from_thread(status.update, f"{marker} {detail}")

    @on(Button.Pressed, "#test-notif")
    def _on_test_notif(self) -> None:
        notifications = self._collect_notifications()
        identity = self.query_one("#identity", Input).value.strip()
        if _channel_summary(notifications) == "no channels":
            self.notify("No channels configured to test.", severity="warning")
            return
        self.notify("Sending test notifications ...")
        self._run_notif_test(notifications, identity)

    @work(thread=True)
    def _run_notif_test(self, notifications: dict[str, Any], identity: str) -> None:
        results = _send_test(notifications, identity)
        for label, ok, err in results:
            if ok:
                self.app.call_from_thread(
                    self.notify, f"\u2713 Sent test to {label}.", severity="information"
                )
            else:
                self.app.call_from_thread(
                    self.notify, f"\u2717 {label}: {err}", severity="error"
                )


class MainScreen(Screen):
    """Dashboard listing all validators and their alerting setup."""

    BINDINGS = [
        ("a", "add", "Add validator"),
        ("r", "refresh", "Refresh checks"),
        ("c", "install_cron", "Install cron"),
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Live status grid \u2014 [bold green]✓ ok[/bold green] / "
            "[bold red]✗ alarm[/bold red] / [dim]— off[/dim].  "
            "[b]Enter[/b] edit row · [b]a[/b] add · [b]r[/b] refresh · "
            "[b]c[/b] cron · [b]q[/b] quit.",
            classes="intro",
        )
        yield DataTable(id="grid", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self._build_table()
        self._run_checks()

    def _build_table(self) -> None:
        table = self.query_one("#grid", DataTable)
        table.clear(columns=True)
        table.add_column("Validator", key="validator")
        for name, label in _WATCHER_COLUMNS.items():
            table.add_column(label, key=name)
        for index, validator in enumerate(self.app.config["validators"]):
            name = (
                validator.get("name")
                or validator.get("identity_pubkey")
                or "validator"
            )
            cluster = validator.get("cluster", "?")
            first = f"{name}  [dim]({cluster})[/dim]"
            table.add_row(
                first,
                *[_CELL_CHECKING for _ in _WATCHER_COLUMNS],
                key=f"val-{index}",
            )

    @work(thread=True, exclusive=True, group="checks")
    def _run_checks(self) -> None:
        """Evaluate every watcher for every validator and fill in the grid.

        Runs in a background thread so the UI stays responsive; each cell is
        updated as its check completes for a live feel.
        """
        table = self.query_one("#grid", DataTable)
        for index, validator in enumerate(self.app.config["validators"]):
            row_key = f"val-{index}"
            try:
                norm = normalize_validator(validator)
            except Exception:  # noqa: BLE001
                norm = validator
            for name in _WATCHER_COLUMNS:
                runner = WATCHER_RUNNERS.get(name)
                try:
                    cell = _status_cell(runner(norm, {}))
                except Exception:  # noqa: BLE001 - a failing check shows as "!"
                    cell = "[yellow]! err[/yellow]"
                try:
                    self.app.call_from_thread(
                        table.update_cell, row_key, name, cell
                    )
                except Exception:  # noqa: BLE001 - row may have been rebuilt
                    pass

    def _refresh_list(self) -> None:
        self._build_table()
        self._run_checks()

    def action_refresh(self) -> None:
        self._build_table()
        self._run_checks()
        self.app.notify("Re-running checks ...")

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        if key.startswith("val-"):
            self._edit(int(key.split("-", 1)[1]))

    def action_add(self) -> None:
        new_validator = {
            "name": "",
            "cluster": "mainnet-beta",
            "identity_pubkey": "",
            "vote_pubkey": "",
        }
        self.app.push_screen(ValidatorScreen(new_validator, is_new=True), self._after_add)

    def _edit(self, index: int) -> None:
        validator = self.app.config["validators"][index]

        def _after_edit(result: Any) -> None:
            if not result:
                return
            action, payload = result
            if action == "save":
                self.app.config["validators"][index] = normalize_validator(payload)
                self.app.persist()
                self._saved_with_cron("Saved.")
            elif action == "delete":
                del self.app.config["validators"][index]
                self.app.persist()
                self.app.notify("Deleted.")
            self._refresh_list()

        self.app.push_screen(ValidatorScreen(validator, is_new=False), _after_edit)

    def _after_add(self, result: Any) -> None:
        if not result:
            return
        action, payload = result
        if action == "save":
            self.app.config["validators"].append(normalize_validator(payload))
            self.app.persist()
            self._saved_with_cron("Validator added.")
            self._refresh_list()

    def _saved_with_cron(self, saved_message: str) -> None:
        """Persisted already; ensure the cron job is installed and notify."""
        if self.app.ensure_cron():
            self.app.notify(f"{saved_message} Monitoring cron is active.")
        else:
            self.app.notify(
                f"{saved_message} (Cron not updated \u2014 press 'c' to retry.)",
                severity="warning",
            )

    def action_install_cron(self) -> None:
        try:
            cmd = install_cron(
                self.app.config_path,
                os.environ.get("PYTHON_BIN") or sys.executable,
                Path(DEFAULT_LOG_PATH),
            )
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Cron install failed: {exc}", severity="error")
            return
        self.notify("Installed cron job (runs every minute).")
        self.app.log(cmd)


class WatcherTUI(App):
    """Top-level Textual application."""

    TITLE = "SolScope Validator Watcher"
    SUB_TITLE = f"v{__version__}"
    CSS = """
    .intro { padding: 1 2; color: $text-muted; }
    .section { padding: 1 0 0 0; color: $accent; text-style: bold; }
    .subsection { padding: 1 0 0 0; color: $secondary; }
    #form { padding: 0 2; }
    .row { height: auto; padding: 1 0; }
    .buttons { padding: 1 0 2 0; }
    .buttons Button { margin: 0 1 0 0; }
    .status { padding: 1 0 0 2; }
    .watch-row { height: auto; padding: 0 0; }
    .watch-label { padding: 1 1 0 1; }
    .cooldown-label { padding: 1 1 0 2; color: $text-muted; }
    .cooldown-input { width: 12; }
    DataTable { height: 1fr; margin: 0 1; }
    Input { margin: 0 0 1 0; }
    """

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        raw = _read_json(config_path, {})
        self.config = normalize_config(raw)
        self._state_file = raw.get("state_file") if raw else None

    def on_mount(self) -> None:
        self.push_screen(MainScreen())

    def persist(self) -> None:
        payload: dict[str, Any] = {"validators": self.config["validators"]}
        if self._state_file:
            payload["state_file"] = self._state_file
        elif self.config.get("state_file"):
            payload["state_file"] = self.config["state_file"]
        _write_json(self.config_path, payload)

    def ensure_cron(self) -> bool:
        """Install/refresh the one-minute cron job (idempotent).

        Called after saving a validator so monitoring is active without the user
        having to remember to install the cron separately. Warns (but doesn't
        block the save) if the crontab can't be updated.
        """
        try:
            install_cron(
                self.config_path,
                os.environ.get("PYTHON_BIN") or sys.executable,
                Path(DEFAULT_LOG_PATH),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Cron update failed: {exc}", severity="warning")
            return False


def run(config_path: Path) -> int:
    WatcherTUI(config_path).run()
    return 0
