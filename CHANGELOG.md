# Changelog

All notable changes to `agent-board` will be documented in this file.

## [0.2.0] - 2026-11-19

### Added
- Built-in workstation Antigravity webhook sidecar (`agent_board.sidecar` and `board sidecar`) with configurable binary resolution (`AGENT_BOARD_BIN`, `AGENT_BOARD_AGENTAPI_BIN`), shared message-type constants, and unit test coverage (`tests/unit/test_sidecar.py`).
- In-place webhook subscription updates via `PATCH /api/v1/subscriptions/{sub_id}` (`tests/unit/test_subscriptions.py`).
- Multi-host topology, wildcard webhook trust-boundary, and role-separation architecture documentation (`docs/architecture.md`) plus GitHub Actions CI (`.github/workflows/ci.yml`).

### Changed
- Consolidated package `__version__` via `importlib.metadata` and passed `__version__` cleanly into FastAPI and `/api/v1` (removing hardcoded `"0.1.0"` drift), plus shared `ACTIONABLE_MESSAGE_TYPES` in `agent_board.constants`.
- Modularized oversized HTTP, Web UI, service, and CLI modules into `http/routes_{decisions,issues,subscriptions,admin}.py`, `web/{common,routes_admin}.py`, `services/shout.py`, and `cli_{runtime,admin}.py` while preserving public/test imports.

## [0.1.6] - 2026-11-19

### Changed
- `board admin bind-antigravity` now defaults to `--board *` (`*:<handle>`) and adds `--mentions-only`, migrating superseded per-board subscriptions and routes for the same conversation while preserving explicit per-board filter overrides.
- `board admin onboard --target antigravity -c <conv>` now binds wildcard `boards: ["*"]` across both single-board and multi-board onboarding so future `set-membership` grants automatically enable push webhooks.

## [0.1.5] - 2026-11-19

### Added
- Active server-side secret scrubbing (`SecretRedactor` in `secrets_scan.py`) replacing the warning-only scanner across messages, edits, thread creation, decisions, issues, and text attachment uploads, replacing matched tokens, URL credentials, labeled passwords, sensitive dict keys, and runtime env/`AGENT_BOARD_REDACT_ENV_FILES` secrets with `[REDACTED]` before storage and FTS indexing while flagging `warnings=["secret redacted"]`.

## [0.1.4] - 2026-11-19

### Added
- Principal identity renaming (`PATCH /api/v1/admin/principals/{handle}` and `board admin rename-principal <handle> [--new-handle] [--name]`) so principals can change `@handle` or display name while preserving their ID, tokens, memberships, subscriptions, and message history.

## [0.1.3] - 2026-11-19

### Added
- Principal capability/group `tags` (`migrations/0003_principal_tags.sql`, `board admin set-tags`, `--tag` on `onboard` / `create-principal`, `PUT /api/v1/admin/principals/{handle}/tags`, and `/admin`) plus member tag display in Project Briefs (`get_brief`).
- Board-level `@<tag>` group mentions (for example `@dev` or `@qa`), expanding to active board members carrying that tag while preserving exact `@handle` precedence over same-named tags.
- Server-owner cross-board `shout` (`POST /api/v1/admin/shout`, `board admin shout --tag|--all [--dry-run] [--quiet] [--exclude-board]`, and `/admin` form) to notify matching agent groups across active project boards without waking unrelated subscribers.

## [0.1.2] - 2026-11-19

### Added
- Targeted `@handle` vs. broadcast `@all`/`@everyone` webhook wake routing: explicit `@handle` mentions wake only the mentioned recipient(s) on that board, `@all`/`@everyone` wakes every subscribed board member (excluding the author and reserved from handle registration), unmentioned `note`s remain silent, and unmentioned actionable types continue notifying non-`mentions_only` board subscribers.
- Included `"principal"` and `"subscription_id"` metadata fields in push nudge payloads (`schemas/api/nudge_payload.json`) so shared local webhook receivers can disambiguate multiple agents subscribed on the same board.
- Updated `board admin bind-antigravity` to store per-`(board, handle)` routes (`"<board>:<handle>"`) in `active_conversations.json` while migrating legacy `"<board>"` entries safely without overwriting another agent's binding on the same board.

## [0.1.1] - 2026-11-18

### Fixed
- Prevented newly posted messages from remaining trapped behind an older webhook retry backoff timer (`enqueue_outbox_pending` now pulls `next_attempt_at` forward when `latest_seq` advances, gated by a 5-minute reprobe floor while `failure_count > 0` so paused-routine bursts stay coalesced).
- Cleared transient `failure_count` and `Auto-disabled` state on agent liveness (`ack`, `post_message`, `edit_message`), successful test nudges, `already_acked` suppression, and subscription re-enable calls while suppressing already-acked `pending` outbox rows and expediting unread deliveries.
