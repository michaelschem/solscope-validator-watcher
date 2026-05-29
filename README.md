# solscope-validator-watcher

Cron-friendly Solana validator monitoring you can run on your own validator host.
It reproduces the alerting that [SolScope](https://github.com/michaelschem/solscope)
provided as a hosted service, with no server or database required: a config file, a
state file for cooldowns, and a one-minute cron job.

It ships with a full-screen terminal UI (built on [Textual](https://textual.textualize.io))
for managing **multiple validators** — each with its own watchers and notification
channels — plus a non-interactive `run-once` command for cron.

## Watchers

| Watcher | What it checks |
|---|---|
| `sfdp_version` | Your node's version against the SFDP **required** minimum (`agave_min_version` from `api.solana.org`). |
| `software_outdated` | Your node's version against the **latest** Agave release on GitHub (`anza-xyz/agave`, matched by Mainnet/Testnet release name). |
| `delinquent` | Whether your vote account is reported delinquent via `getVoteAccounts`. |

Each watcher has its own `cooldown_minutes` so a per-minute cron won't spam you while
a condition persists. Cooldown state is tracked in a JSON state file alongside the config.

## Notification channels

Configure any combination under `notifications`:

- `slack_webhooks` — Slack incoming webhook URLs
- `discord_webhooks` — Discord webhook URLs
- `webhooks` — generic webhooks (`{"text": "..."}` POST body)
- `ntfy_topics` — [ntfy.sh](https://ntfy.sh) topics
- `pagerduty_integration_keys` — PagerDuty Events API v2 routing keys
- `twilio` — Twilio SMS. You must supply your **own** Twilio sending source (`account_sid`, `auth_token`, and a verified `from_phone`) plus the destination `to_phones`. Unlike the hosted SolScope service, there is no shared sender — SMS only works if you provide your own Twilio account.
- `smtp_email` — SMTP email (`host`, `port`, `username`, `password`, `from_email`, `to_emails`, `use_tls`)

## Install

From PyPI:

```bash
python3 -m pip install solscope-validator-watcher
```

From source (this directory):

```bash
python3 -m pip install .
```

Editable install for development:

```bash
python3 -m pip install -e .
```

## Configure (the TUI)

The TUI is the primary entrypoint. Just run the command with no arguments:

```bash
solscope-validator-watcher
```

By default everything lives under `~/.solscope-validator-watcher/`
(`config.json`, the cooldown state file, and `watcher.log`), so no root access is
required. Use `--config <path>` to point at a different file.

In the dashboard you can:

- See every configured validator at a glance — cluster, identity, which watchers are
  enabled, and which notification channels will alert.
- Press **`a`** to add a validator, or **Enter** on a row to edit it.
- In the editor: set cluster (or a custom RPC URL), identity/vote keys, toggle each
  watcher and its cooldown, and fill in notification channels.
  - **`Ctrl+S`** save · **`Ctrl+T`** send test notifications · **`Ctrl+D`** delete · **`Esc`** cancel
  - **Test RPC** button verifies connectivity to the endpoint before you save.
- Press **`c`** to install the one-minute cron job that runs the watchers.
- Press **`q`** to quit.

Prefer to edit by hand? Copy [`config.example.json`](./config.example.json) and edit it.

### Custom RPC endpoint

By default each validator uses the public endpoint for its cluster
(`https://api.<cluster>.solana.com`). Set a per-validator `rpc_url` to use your own RPC —
the `cluster` value is still used for the version checks. Leave it `null`/blank for the
public endpoint.

## Run (cron)

The watchers run via the non-interactive `run-once` command, which the TUI's "install
cron" action wires up for you. To do it manually:

```bash
solscope-validator-watcher run-once          # run all validators once (used by cron)
solscope-validator-watcher install-cron      # install the one-minute cron job
```

`install-cron` accepts `--config`, `--python-bin`, and `--log-file`.

## Recommended: high-availability setup

A monitor is only useful if it's running when something breaks — so don't rely on a
single host to watch itself. Because one config can hold **multiple validators**, the
most resilient setup is:

1. Add **both** your mainnet and testnet validators to the config (in the TUI, press `a`
   twice).
2. Install the watcher + cron on **both** hosts, each running that same config.

That way every validator is observed by two independent hosts. If one host goes down (the
exact moment you most want an alert), the other host is still checking it and will fire the
notification. The per-watcher cooldowns keep the two hosts from double-alerting you into
noise.

## License

MIT — see [LICENSE](./LICENSE).
