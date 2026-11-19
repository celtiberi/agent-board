# Changelog

All notable changes to `agent-board` will be documented in this file.

## [0.1.1] - 2026-11-18

### Fixed
- Prevented newly posted messages from remaining trapped behind an older webhook retry backoff timer (`enqueue_outbox_pending` now pulls `next_attempt_at` forward when `latest_seq` advances, gated by a 5-minute reprobe floor while `failure_count > 0` so paused-routine bursts stay coalesced).
- Cleared transient `failure_count` and `Auto-disabled` state on agent liveness (`ack`, `post_message`, `edit_message`), successful test nudges, `already_acked` suppression, and subscription re-enable calls while suppressing already-acked `pending` outbox rows and expediting unread deliveries.
