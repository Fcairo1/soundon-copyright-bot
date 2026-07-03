# SoundOn Copyright Alert Bot

SoundOn Copyright Alert Bot automates copyright-claim intake, regional filtering, Lark notifications, tracker updates, operator follow-up, and manager escalation for BR, SPLA, and US workflows.

## What the project does

At a high level, the bot:

1. Scans infringement-claim emails from a dedicated mailbox.
2. Extracts claim metadata such as UPC, ISRC, claimant, and DSP details.
3. Checks Aeolus data to determine region relevance and release quality.
4. Posts actionable Lark cards to the correct regional chat or DM.
5. Updates tracker sheets and supports operator actions from interactive cards.
6. Escalates overdue cases to managers and prepares reply drafts when needed.

## Project layout

The project is a proper Python package. Production modules live in the
`copyright_alert/` package and are imported as `copyright_alert.<module>`.
Operational, diagnostic, and one-off scripts live under `scripts/`. All
generated state is written to `runtime/` (gitignored); logs go to `logs/`.

```text
soundon-copyright-bot/
├── copyright_alert/            # importable production package (copyright_alert.<module>)
│   ├── __init__.py
│   ├── run_alert.py            # core single-run scan/parse/post engine
│   ├── daily_workflow.py       # main scheduled workflow (primary entry point)
│   ├── bot_runtime.py          # shared runtime helpers, region config, daemon control
│   ├── persistent_callback.py  # long-running Lark callback/websocket daemon
│   ├── handle_callback.py      # card action -> tracker write-back
│   ├── dm_action_card.py       # build/send DM action cards
│   ├── dm_upc_lookup.py        # DM-driven UPC lookup
│   ├── lark_auth.py            # Lark auth/token-refresh helpers
│   ├── lark_mail_draft.py      # Lark Mail draft API helper
│   ├── spotify_reply.py        # Spotify reply-draft workflow
│   ├── oauth_setup.py          # one-time Lark Mail OAuth bootstrap
│   ├── region_guard.py         # wrong-chat safety checks
│   ├── watchdog.py             # callback-daemon health/restart monitor
│   ├── tag_managers.py         # manager-escalation workflow
│   ├── manager_alert_region.py # shared regional escalation logic
│   ├── manager_alert_spla.py   # SPLA manager-alert entry point
│   ├── manager_alert_us.py     # US manager-alert entry point
│   ├── manager_exclusions.py   # manager-tagging exclusions (config)
│   ├── manager_exclusions.json # committed exclusions config
│   └── dm_overdue_filipe.py    # BR overdue-case digest
├── scripts/
│   ├── backfill/               # bulk backfill & data-maintenance utilities
│   ├── diagnostics/            # diagnostics, recovery, verification tools
│   └── one_off/                # resend/repost/retry & manual regional runners
├── runtime/                    # generated state (gitignored, kept via .gitkeep)
├── logs/                       # log output (gitignored, kept via .gitkeep)
├── requirements.txt
├── pyproject.toml
├── .env.example
└── README.md
```

## Setup

### 1. Python & dependencies

Requires **Python 3.9+** (`zoneinfo`). Install dependencies:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # installs lark-oapi
```

### 2. Environment variables

Copy `.env.example` to `.env` and fill in the values, then load it
(`set -a; . ./.env; set +a`). `.env` is gitignored — never commit it.

| Variable | Required | Purpose |
| --- | --- | --- |
| `BOT_SECRET` | ✅ | App secret for the Lark copyright bot (`cli_aa94690b12b81cde`). Read first by `run_alert.py`. |
| `LARK_APP_SECRET` | ✅ (fallback) | Used if `BOT_SECRET` is unset. |
| `INNER_SKILLS_DIR` | ✅ (prod) | Absolute path to the AIME `inner_skills/` directory providing `feishu-im-send` and `aeolus-platform-analysis` helper scripts. Defaults to `<repo>/inner_skills` if unset. |
| `LARK_MAILBOX` | optional | Mailbox override for environments that do not want the default inbox baked into the code. |
| `DEFAULT_REGION` | optional | Default region override for wrapper scripts or future deployment config. |

### 3. External tools (not installed via pip)

- **`lark-cli`** — the Lark/Feishu CLI binary must be on `PATH`. Used for mailbox
  triage, sheet read/write, and mail draft surfaces.
- **`lark_oapi`** — Python SDK (installed via `requirements.txt`); used by the
  callback websocket daemon.
- **AIME `inner_skills`** — the `feishu-im-send` and `aeolus-platform-analysis`
  skill directories are provided by the AIME runtime and located via
  `INNER_SKILLS_DIR`. They are not part of this repo.

### 4. Secrets & generated files (never committed)

- `runtime/lark_mail_oauth.json` — Lark Mail OAuth token (created by `oauth_setup.py`).
- `runtime/aime_env_refresh.json` — cached env-refresh state.
- `secrets.json`, `.env` — credentials.

Run the one-time Lark Mail OAuth bootstrap when first enabling draft access:

```bash
python -m copyright_alert.oauth_setup
```

## Running

All entry points run as modules from the **repo root** (so `copyright_alert` is
importable and `runtime/` / `logs/` resolve correctly):

```bash
# Main scheduled daily workflow (primary entry point)
python -m copyright_alert.daily_workflow

# Single-run scan/post engine
python -m copyright_alert.run_alert

# Long-running Lark callback daemon
python -m copyright_alert.persistent_callback

# Manager escalation
python -m copyright_alert.tag_managers
```

Operational scripts run the same way, addressed by their package path:

```bash
python -m scripts.backfill.backfill_ap_direitos_br
python -m scripts.diagnostics.diagnose_br_skips
python -m scripts.one_off.scan_spla_first
```

## Main workflow

```text
Mailbox scan -> metadata extraction -> Aeolus lookup -> region validation
-> Lark group/DM card posting -> tracker updates -> operator action / manager escalation
```

### Primary entry points

| Module | Role |
| --- | --- |
| `copyright_alert.daily_workflow` | Main scheduled workflow for daily scans, reminders, and tracker-driven follow-up. |
| `copyright_alert.run_alert` | Core single-run scanning engine that reads inbox messages, filters candidates, and posts alert cards. |
| `copyright_alert.persistent_callback` | Long-running callback daemon that handles Lark card actions, slash commands, and interactive follow-up flows. |
| `copyright_alert.tag_managers` | Scheduled escalation script that tags responsible managers for overdue or pending cases. |

## Module reference

### `copyright_alert/` — core orchestration and shared runtime

| Module | Purpose |
| --- | --- |
| `run_alert` | Core alert engine for mailbox scanning, claim parsing, Aeolus lookups, candidate filtering, and Lark alert posting. |
| `daily_workflow` | Coordinates recurring scan cycles, reminder logic, tracker maintenance, and daily operational tasks. |
| `bot_runtime` | Shared runtime helpers for configuration loading, region settings, Lark integrations, and long-running bot process support. |
| `lark_auth` | Handles Lark authentication helpers, token refresh, and API auth error recovery. |
| `oauth_setup` | One-time OAuth bootstrap utility for enabling Lark Mail draft access. |
| `region_guard` | Safety checks that prevent alerts from being posted into the wrong regional chat. |
| `watchdog` | Monitors the callback daemon and helps restart or recover it when unhealthy. |
| `persistent_callback` | WebSocket/event-loop daemon for Lark card callbacks, slash commands, and interactive operator workflows. |
| `handle_callback` | Processes card button clicks and writes status or ownership updates back to tracker sheets. |
| `dm_action_card` | Builds and sends direct-message action cards so operators can handle claims with one-click actions. |
| `dm_upc_lookup` | Supports DM-driven UPC lookup flows to find matching claims in the inbox. |
| `lark_mail_draft` | Low-level helper for creating threaded email drafts through the Lark Mail API. |
| `spotify_reply` | Higher-level reply workflow that prepares operator-facing Spotify response drafts such as agree, investigate, or dispute. |
| `tag_managers` | Main manager-escalation workflow that scans trackers and @-mentions responsible managers in chat. |
| `manager_alert_region` | Shared region-specific escalation logic reused by manager alert runners. |
| `manager_alert_spla` | Entry point for SPLA manager alert execution. |
| `manager_alert_us` | Entry point for US manager alert execution. |
| `manager_exclusions` | Stores and applies manager-tagging exclusions for specific people or labels. |
| `dm_overdue_filipe` | Sends overdue-case digests for long-open items to the BR Ops owner. |

### `scripts/backfill/` — backfill and data maintenance

| Module | Purpose |
| --- | --- |
| `backfill_ap_direitos_br` | Bulk BR backfill tool that reconstructs or posts historical alerts using batched lookups. |
| `backfill_ap_direitos_br_concurrent` | Concurrent wrapper/variant for the BR backfill process. |
| `backfill_after_anchor` | Backfills alerts created after a known-good production anchor. |
| `backfill_dsp_fields` | Fills missing DSP-related metadata in historical records. |
| `append_tracker_046081232374` | One-off utility to append a specific UPC into the tracker manually. |

### `scripts/diagnostics/` — diagnostics, recovery, verification

| Module | Purpose |
| --- | --- |
| `test_spotify_flow` | End-to-end test harness for the Spotify reply and DM action-card workflow. |
| `diagnose_br_skips` | Investigates why specific BR cases were skipped by automated scans. |
| `us_test_alert` | Test runner for validating the US scan-and-post flow. |
| `us_post_specific` | Diagnostic tool that posts one specific US case for verification. |
| `post_us_candidate` | Posts a US candidate found by a previous scan step. |
| `query_aeolus_individual` | Manual inspection tool for querying Aeolus data by individual UPCs. |
| `reconstruct_generic_scan` | Reconstructs scan behavior for diagnostics by replaying metadata extraction logic. |
| `reproduce_batch_bug` | Minimal reproduction script for debugging batch-query issues. |

### `scripts/one_off/` — resend/repost/retry & manual regional runners

| Module | Purpose |
| --- | --- |
| `scan_us_candidates` | Searches for qualifying US-region claims after a known processing anchor. |
| `scan_us_next_after_anchor` | Finds the next single US claim after a chosen anchor point for controlled replay. |
| `scan_spla_first` | Deep scanner that finds and posts the first qualifying SPLA claim. |
| `scan_spla_batch` | Faster SPLA scan path using parallel fetches and batched Aeolus queries. |
| `scan_spla_paginated` | Paginated SPLA scanner for traversing large mailbox history safely. |
| `send_next_after_upc` | Finds and posts the next qualifying alert after a specified UPC. |
| `run_five_recent` | Convenience script that posts the five most recent qualifying alerts. |
| `dm_overdue_filipe_fast` | Faster implementation of the BR overdue digest flow. |
| `run_br_manager_alert_once` | One-off BR manager alert runner built for ad hoc execution and validation. |
| `resend_dm_test` | Resends a DM test card to validate rendering and callback behavior. |
| `resend_5063963471855` | One-off resend script for a specific claim identified by UPC `5063963471855`. |
| `resend_spla_tasks` | Reposts or resends selected SPLA tasks. |
| `resend_047752352704_filipe` | One-off resend script for a specific BR DM card to Filipe. |
| `repost_br_missed_20260701` | Recovery script for BR cards missed during a known incident on 2026-07-01. |
| `retry_br_20260626_failed_cards` | Retries BR card deliveries that failed during a known 2026-06-26 issue. |
| `send_5_dm_cards` | Sends DM action cards for a fixed set of five target cases. |
| `send_ben_dm_card` | One-off helper that sends a DM action card to the US point of contact. |

## Notes

- Production code lives in `copyright_alert/`; operational one-offs, replay tools, diagnostics, and incident-recovery helpers live under `scripts/`.
- Many scripts are intentionally region-specific because BR, SPLA, and US workflows use different chats, trackers, and escalation rules.
- Secrets, OAuth tokens, and credentials must be provided through environment variables or secure local files that are not committed.
- Runtime artifacts (PIDs, callback state, local JSON caches, logs) are written to `runtime/` and `logs/` and are gitignored.
