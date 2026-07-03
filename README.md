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

## Main workflow

### Primary entry points

| File | Role |
| --- | --- |
| `daily_workflow.py` | Main scheduled workflow for daily scans, reminders, and tracker-driven follow-up. |
| `run_alert.py` | Core single-run scanning engine that reads inbox messages, filters candidates, and posts alert cards. |
| `persistent_callback.py` | Long-running callback daemon that handles Lark card actions, slash commands, and interactive follow-up flows. |
| `tag_managers.py` | Scheduled escalation script that tags responsible managers for overdue or pending cases. |

### Typical flow

```text
Mailbox scan -> metadata extraction -> Aeolus lookup -> region validation
-> Lark group/DM card posting -> tracker updates -> operator action / manager escalation
```

## Project structure

### Core orchestration and shared runtime

| File | Purpose |
| --- | --- |
| `run_alert.py` | Core alert engine for mailbox scanning, claim parsing, Aeolus lookups, candidate filtering, and Lark alert posting. |
| `daily_workflow.py` | Coordinates recurring scan cycles, reminder logic, tracker maintenance, and daily operational tasks. |
| `bot_runtime.py` | Shared runtime helpers for configuration loading, region settings, Lark integrations, and long-running bot process support. |
| `lark_auth.py` | Handles Lark authentication helpers, token refresh, and API auth error recovery. |
| `oauth_setup.py` | One-time OAuth bootstrap utility for enabling Lark Mail draft access. |
| `region_guard.py` | Safety checks that prevent alerts from being posted into the wrong regional chat. |
| `watchdog.py` | Monitors the callback daemon and helps restart or recover it when unhealthy. |

### Lark interaction, callbacks, and reply drafting

| File | Purpose |
| --- | --- |
| `persistent_callback.py` | WebSocket/event-loop daemon for Lark card callbacks, slash commands, and interactive operator workflows. |
| `handle_callback.py` | Processes card button clicks and writes status or ownership updates back to tracker sheets. |
| `dm_action_card.py` | Builds and sends direct-message action cards so operators can handle claims with one-click actions. |
| `dm_upc_lookup.py` | Supports DM-driven UPC lookup flows to find matching claims in the inbox. |
| `lark_mail_draft.py` | Low-level helper for creating threaded email drafts through the Lark Mail API. |
| `spotify_reply.py` | Higher-level reply workflow that prepares operator-facing Spotify response drafts such as agree, investigate, or dispute. |

### Regional scans and targeted posting

| File | Purpose |
| --- | --- |
| `scan_us_candidates.py` | Searches for qualifying US-region claims after a known processing anchor. |
| `scan_us_next_after_anchor.py` | Finds the next single US claim after a chosen anchor point for controlled replay. |
| `scan_spla_first.py` | Deep scanner that finds and posts the first qualifying SPLA claim. |
| `scan_spla_batch.py` | Faster SPLA scan path using parallel fetches and batched Aeolus queries. |
| `scan_spla_paginated.py` | Paginated SPLA scanner for traversing large mailbox history safely. |
| `send_next_after_upc.py` | Finds and posts the next qualifying alert after a specified UPC. |
| `run_five_recent.py` | Convenience script that posts the five most recent qualifying alerts. |

### Manager escalation and overdue follow-up

| File | Purpose |
| --- | --- |
| `tag_managers.py` | Main manager-escalation workflow that scans trackers and @-mentions responsible managers in chat. |
| `manager_alert_region.py` | Shared region-specific escalation logic reused by manager alert runners. |
| `manager_alert_spla.py` | Entry point for SPLA manager alert execution. |
| `manager_alert_us.py` | Entry point for US manager alert execution. |
| `manager_exclusions.py` | Stores and applies manager-tagging exclusions for specific people or labels. |
| `dm_overdue_filipe.py` | Sends overdue-case digests for long-open items to the BR Ops owner. |
| `dm_overdue_filipe_fast.py` | Faster implementation of the BR overdue digest flow. |
| `run_br_manager_alert_once.py` | One-off BR manager alert runner built for ad hoc execution and validation. |

### Backfill and data maintenance utilities

| File | Purpose |
| --- | --- |
| `backfill_ap_direitos_br.py` | Bulk BR backfill tool that reconstructs or posts historical alerts using batched lookups. |
| `backfill_ap_direitos_br_concurrent.py` | Concurrent wrapper/variant for the BR backfill process. |
| `backfill_after_anchor.py` | Backfills alerts created after a known-good production anchor. |
| `backfill_dsp_fields.py` | Fills missing DSP-related metadata in historical records. |
| `append_tracker_046081232374.py` | One-off utility to append a specific UPC into the tracker manually. |

### Diagnostics, recovery, and one-off operational scripts

| File | Purpose |
| --- | --- |
| `test_spotify_flow.py` | End-to-end test harness for the Spotify reply and DM action-card workflow. |
| `diagnose_br_skips.py` | Investigates why specific BR cases were skipped by automated scans. |
| `us_test_alert.py` | Test runner for validating the US scan-and-post flow. |
| `us_post_specific.py` | Diagnostic tool that posts one specific US case for verification. |
| `post_us_candidate.py` | Posts a US candidate found by a previous scan step. |
| `resend_dm_test.py` | Resends a DM test card to validate rendering and callback behavior. |
| `resend_5063963471855.py` | One-off resend script for a specific claim identified by UPC `5063963471855`. |
| `resend_spla_tasks.py` | Reposts or resends selected SPLA tasks. |
| `resend_047752352704_filipe.py` | One-off resend script for a specific BR DM card to Filipe. |
| `repost_br_missed_20260701.py` | Recovery script for BR cards missed during a known incident on 2026-07-01. |
| `retry_br_20260626_failed_cards.py` | Retries BR card deliveries that failed during a known 2026-06-26 issue. |
| `query_aeolus_individual.py` | Manual inspection tool for querying Aeolus data by individual UPCs. |
| `reconstruct_generic_scan.py` | Reconstructs scan behavior for diagnostics by replaying metadata extraction logic. |
| `reproduce_batch_bug.py` | Minimal reproduction script for debugging batch-query issues. |
| `send_5_dm_cards.py` | Sends DM action cards for a fixed set of five target cases. |
| `send_ben_dm_card.py` | One-off helper that sends a DM action card to the US point of contact. |

## Notes

- The repository mixes core production scripts with operational one-offs, replay tools, diagnostics, and incident recovery helpers.
- Many scripts are intentionally region-specific because BR, SPLA, and US workflows use different chats, trackers, and escalation rules.
- Secrets, OAuth tokens, and other credentials should be provided through environment variables or secure local files that are not committed.
- Runtime artifacts such as PIDs, callback state, and local JSON caches should stay out of committed changes unless intentionally versioned.
