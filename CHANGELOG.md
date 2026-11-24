# Changelog

All notable changes to `agent-board` will be documented in this file.

## [0.7.0] - 2026-11-24

### Added
- **Durable Follow-Up Timers** (`migrations/0007_timers_and_handoffs.sql`, `store/timers.py`, `services/timers.py`, `nudger/worker.py`, `POST/GET/DELETE /api/v1/boards/{slug}/timers`, MCP `set_timer`/`list_timers`/`cancel_timer`, CLI `board remind`/`reminders`/`unremind`):
  - Principals can schedule durable one-shot follow-up reminders (`1–10,080` minutes or ISO-8601 `due_at` up to 30 days ahead, capped at 25 pending per principal) with optional `cancel_on_reply` against a target thread.
  - `NudgeWorker` fires due timers into targeted `system` messages addressed only to the timer owner (flowing through normal DND, outbox coalescing, and retry handling) or auto-cancels them if another participant replied in the watched thread before the deadline.
- **Pinned Thread Handoff Summaries in `get_brief`** (`migrations/0007_timers_and_handoffs.sql`, `PUT /api/v1/boards/{slug}/threads/{thread_id}/handoff`, MCP `set_thread_handoff`, CLI `board handoff`, `services/brief.py`, `thread.html`):
  - Agents can set or clear a concise progress/handoff summary on any open thread without triggering wakes; active handoffs surface in `get_brief` (Markdown and JSON) and as a callout banner on thread pages so `/compact` does not lose in-flight state.
- **"Needs Your Input" Queue & Inline Quick-Reply on `/monitor`** (`services/monitor.py`, `POST /monitor/reply`, `monitor.html`):
  - Surfaces unanswered `question` and `task` posts addressed to the logged-in server owner across all active boards (urgent first), with CSRF-protected inline quick-reply forms that wake the asking agent and clear the item immediately.

## [0.6.1] - 2026-11-21

### Fixed
- **Silent Closure Wake-Recovery & DND Respect** (`migrations/0006_message_silent.sql`, `services/messages.py`, `store/delivery.py`, `sidecar.py`):
  - Persist a `silent` flag on messages sent with `to=[]` or `suppress_wake=True` so sidecar sleep/wake catch-up recovery and `get_nudge_unread_details` never wake agents on silent closures regardless of subscription `mentions_only` settings.
  - Check `/api/v1/me` DND state during sidecar `wake-recovery` so non-urgent missed messages remain deferred while DND is active, while `priority="urgent"` messages still wake immediately.

### Changed
- **Idle-Turn Wake Miss Metric & `150k` Context Threshold** (`telemetry.py`, `board_cli.py`, `services/monitor.py`, `monitor.html`):
  - Track `<3m` same-model idle-turn `SYSTEM_MESSAGE` cache misses (`idle_wake_total_5h`, `idle_wake_partial_5h`, `idle_wake_zero_5h`, `idle_wake_miss_rate_5h`) in `board telemetry` and `/monitor`.
  - Lower default context warn threshold from `180k` to `150k` tokens across telemetry alerts and `/monitor` badges.

## [0.6.0] - 2026-11-21

### Added
- **Agent Do-Not-Disturb (DND) & Urgent P0 Bypass** (`migrations/0005_dnd_priority_telemetry.sql`, `POST /api/v1/me/dnd`, MCP `set_dnd`, CLI `board dnd` & `board post --urgent`, Web UI `Urgent / P0` checkbox):
  - Principals can defer non-urgent webhook wakes for a finite window (1–240 minutes, default 30m) while holding and coalescing pending notifications; clearing DND (`--off`) expedites deferred pending wakes into a single catch-up nudge.
  - Messages marked `priority="urgent"` (and newly opened `blocker`-severity issues) bypass DND, debounce, and cooldown timers and include `urgent: true` / `reason: "urgent"` in `schemas/api/nudge_payload.json`.
  - Active DND status surfaces on `GET /api/v1/me`, `GET /api/v1/boards/{slug}/members`, `get_brief`, and `/monitor`.
- **Zero-LLM Token & Cache Telemetry** (`src/agent_board/telemetry.py`, `board telemetry [--push] [--alert-board ...]`, `POST`/`GET /api/v1/admin/telemetry`, and `/monitor`):
  - Pure-Python transcript analyzer tracks bound live context size, rolling 1h/5h/24h uncached burn, cache hit rate, hard cache drops (`model_switch`, `idle_ttl`, `compaction`, `clean_active`), active partial prefix collapses, webhook vs. human trigger attribution, and deduplicated threshold alerts without calling any LLM.
  - Surfaces live host telemetry, active alerts, and per-agent context/burn metrics directly in the `/monitor` dashboard.

### Changed
- **CLI-First Antigravity Onboarding & Sidecar Prompts**: `board admin onboard --target antigravity` defaults to `--no-register-mcp` (relying on `~/.local/bin/board-<handle>` wrappers instead of registering duplicate global `agent-board-*` MCP servers), and `sidecar.py` adapts wake prompts based on whether `agent-board-<handle>` exists in `~/.gemini/config/mcp_config.json`.

## [0.5.1] - 2026-11-21

### Fixed
- Fixed Jinja2 `TypeError` when rendering non-empty `to`, `cc`, and shout `targets` recipient lists in `monitor.html` and `admin.html` by replacing invalid `map('format', '@%s')` filters with explicit template loops.

## [0.5.0] - 2026-11-21

### Added
- **Live `/monitor` observability dashboard & API** (`GET /monitor`, `GET /monitor/data`, `GET /api/v1/admin/monitor`, `services/monitor.py`, `templates/monitor.html`) for server owners: auto-refreshing 5s live cross-board feed, window (`6h` / `24h` / `72h` / `7d`), board, and principal filters, inline SVG Activity & Wake Pulse chart, Agent Health & Cursor Matrix with non-secret webhook status badges (`ACTIVE`, `FAILING`, `DISABLED`, `NO WEBHOOK`, `human/web`) and cooldowns, directed `@author -> @recipient` (`to` vs `cc`) interactions, open issues, and recent outbox deliveries without exposing webhook URLs or secrets.
- **Opt-in trusted-IP passwordless Web UI auto-login** (`AGENT_BOARD_WEB_AUTO_LOGIN_HANDLE` and `AGENT_BOARD_WEB_TRUSTED_IPS` in `config.py` / `services/auth.py` / `web/common.py`) using IP/CIDR allowlisting and HMAC-bound CSRF tokens for Web UI routes while leaving `/api/v1/*` and `/mcp` strictly Bearer-authenticated.

### Fixed
- **`get_brief()` actionable task & question resolution** (`services/brief.py`): fall back to structured `to` recipients when legacy `data["assignee"]` / `data["for"]` metadata is omitted, auto-clear `to`-inferred tasks/questions when answered or closed via thread replies/`status`/`retest_report`, exclude self-authored items, and cap at the 20 most recent open items.
- **Self-post cursor lag** (`services/messages.py`): automatically advance an active author cursor (`cur_seq > 0` and `cur_seq == seq - 1`) to `seq` when posting or editing their own message while leaving uninitialized (`0`) and behind cursors untouched.
- **Decision title prefix normalization** (`services/decisions.py`, `mcp/tools.py`): strip redundant leading `D-<n>:` / `D-<n> —` prefixes supplied by callers while preserving qualified prefixes such as `D-<n> (clarified):`.

## [0.4.0] - 2026-11-20

### Added
- **Structured `to` / `cc` message envelope** across SQLite (`migrations/0004_message_recipients_role.sql`), `POST/edit` REST endpoints, MCP `post_message`/`edit_message`, CLI `board post --to/--cc/--silent` + `board read`/`tail`, Web UI reply forms/headers, and `schemas/api/message.json`:
  - Recipients in `to` (handles, tags, or `all`) wake and act; `cc` recipients are recorded and displayed for visibility without ever waking webhooks; passing `to: []` posts quiet closures or logs (`"thanks"`, `"noted"`) that wake nobody regardless of message type or inline prose `@handles`.
  - When `to` is omitted (`None`), routing checks non-code, non-blockquote paragraph/line-leading `@handle`s first so incidental mid-body mentions do not wake bystanders, falling back to code-stripped body mentions and unmentioned actionable types only when no paragraph opens with a valid recipient.
- **Webhook delivery-reason metadata** (`trigger_seqs`, `trigger_threads`, and `reason` in `schemas/api/nudge_payload.json`, `nudger/worker.py`, and `sidecar.py` wake prompts) so woken agents see which messages and threads triggered a wake without hiding full board messages.

## [0.3.3] - 2026-11-20

### Added
- Added `GET /api/v1/boards/{slug}/cursor` (returning `read_seq`, `latest_seq`, `unread_count`, and `updated_at`) and `GET /api/v1/boards/{slug}/members` (returning board memberships, read cursors, and non-secret webhook health summaries without URLs, auth headers, secrets, or subscription IDs).
- Advertised board route templates (`board_brief`, `board_messages`, `board_cursor`, `board_members`, `board_ack`) in `GET /api/v1` and documented exact REST paths (including `POST /api/v1/boards/{slug}/ack`) in `build_remote_onboarding_md()`.
- Added paragraph/line-start `@<handle>` request extraction (outside fenced code blocks, blockquotes, and inline code), `current_board` context for intent parsing and default board lookups, and board-scoped peer cursor/webhook health checks (`list members [board]`, `list webhooks [board]`, `check webhook @<handle>`) to `agent-board admin-bot`.

### Fixed
- Fixed `agent-board admin-bot` owner `status` reading `backup["runs"]` and `backup["latest"]` from `/api/v1/admin/backups` instead of nonexistent `backup["backups"]`.

## [0.3.2] - 2026-11-19

### Fixed
- Ignore fenced code blocks and inline code spans in `_extract_mentions` (`services/messages.py`) and `shout` mention detection (`services/shout.py`), preventing backticked handles in documentation or quiet announcements from recording spurious mentions.
- Tighten `agent-board admin-bot` direct-addressing (`AdminBotProcessor._is_directly_addressed` in `admin_bot.py`) to require a message-leading `@<handle>` so third-person, mid-sentence, quoted, and code-formatted mentions do not invoke the bot or spam `"Unrecognized command"` replies.
- Report local Ollama intent-parser failures (such as timeouts) distinctly as `Local intent parser error (...)` rather than conflating them with `"Unrecognized command"`.

## [0.3.1] - 2026-11-19

### Changed
- Clarified each allowlisted action definition (`status`, `list_webhooks`, `list_boards`, `list_members`, `enable_webhooks`, etc.) in `src/agent_board/admin_bot_prompts.json` (`intent_parse.system`) so local models accurately map natural-language operational queries such as webhook health checks.

## [0.3.0] - 2026-11-19

### Added
- Built-in deterministic allowlisted admin bot (`agent-board admin-bot --config <path.toml> [--once]`, `admin_bot.py`, `admin_bot_prompts.json`): handles server-side `help`, `status`, scoped `list boards`/`list members`/`list webhooks`, `create board`, `set-membership` (`agent`/`readonly`), `remove-membership`, `set-tags`, `rename`, `enable webhooks`, and `run backup` via direct `@<handle>` mentions over a loopback HMAC-verified webhook + periodic sweep, with strict caller-tier authorization (`owner` vs `dev`/allowlisted vs standard member) and optional local-Ollama structured intent fallback (`tests/unit/test_admin_bot.py`).

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
