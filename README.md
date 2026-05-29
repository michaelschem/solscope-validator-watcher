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

Install into a virtual environment. This is the recommended approach everywhere,
and on modern Debian/Ubuntu (PEP 668 "externally-managed-environment") it is
required — a system-wide `pip install` will be refused.

```bash
# Create a venv (one-time). Anywhere is fine; this keeps it with the config.
python3 -m venv ~/.solscope-validator-watcher/venv

# Activate it, then install
source ~/.solscope-validator-watcher/venv/bin/activate
pip install solscope-validator-watcher
```

> On Ubuntu you may first need `sudo apt install python3-venv` (and `python3-pip`).

After activating the venv, the `solscope-validator-watcher` command is on your
`PATH`. You don't need to keep the venv activated for cron — see
[Run (cron)](#run-cron), which records the venv's Python automatically.

If you prefer not to manage a venv yourself, [`pipx`](https://pipx.pypa.io) does
it for you:

```bash
pipx install solscope-validator-watcher
```

### From source (for development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
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
cron" action wires up for you. To do it manually (from inside the activated venv):

```bash
solscope-validator-watcher run-once          # run all validators once (used by cron)
solscope-validator-watcher install-cron      # install the one-minute cron job
```

cron does **not** inherit your activated virtualenv, so `install-cron` bakes the
**absolute path of the current Python interpreter** into the cron line. As long as
you run `install-cron` (or press `c` in the TUI) from inside the venv where you
installed the package, the cron job will use that same venv automatically — no
activation needed at run time. The installed line looks like:

```
* * * * * /home/solana/.solscope-validator-watcher/venv/bin/python -m validator_watcher run-once --config "..." >> "..." 2>&1
```

`install-cron` accepts `--config`, `--python-bin` (override the interpreter), and
`--log-file`.

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
