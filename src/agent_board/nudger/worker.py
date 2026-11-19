"""Background outbox worker sending coalesced, signed nudge webhooks."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from agent_board.ids import dt_to_iso, iso_to_dt, new_ulid, utc_now, utc_now_iso
from agent_board.nudger.signing import sign_nudge
from agent_board.services.context import AppServices, system_context
from agent_board.services.delivery import normalize_auth_header
from agent_board.services.messages import post_message_in_tx
from agent_board.store import boards as boards_store
from agent_board.store import delivery as delivery_store

logger = logging.getLogger(__name__)

_BACKOFF_SCHEDULE_S = (30, 120, 600, 1800, 3600, 7200)
_LATE_BACKOFF_S = 21600  # 6 hours
_DEAD_AFTER_S = 86400  # 24 hours
_STALE_LEASE_S = 60


def compute_retry_delay_s(attempt: int) -> int:
    """Return backoff delay in seconds for 1-based failure attempt count."""
    if attempt <= len(_BACKOFF_SCHEDULE_S):
        return _BACKOFF_SCHEDULE_S[max(0, attempt - 1)]
    return _LATE_BACKOFF_S


class NudgeWorker:
    """Processes due outbox nudges with coalescing, HMAC signing, and backoff."""

    def __init__(
        self,
        services: AppServices,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.services = services
        self._transport = transport

    def _build_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {"timeout": self.services.settings.nudge_timeout_s}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def send_test_nudge(self, *, sub_id: str, board_slug: str | None = None) -> dict[str, Any]:
        conn = self.services.db.read_conn()
        sub = delivery_store.get_subscription_by_id(conn, sub_id)
        if sub is None:
            return {"ok": False, "status_code": None, "error": "Subscription not found"}

        resolved_slug = board_slug
        if not resolved_slug:
            sub_boards = [b for b in json.loads(sub["boards"]) if b and b != "*"]
            if sub_boards:
                resolved_slug = sub_boards[0]
            else:
                member_boards = boards_store.list_boards_for_principal(
                    conn,
                    principal_id=sub["principal_id"],
                    is_server_owner=bool(sub["is_server_owner"]),
                    include_archived=False,
                )
                resolved_slug = str(member_boards[0]["slug"]) if member_boards else "ops"

        now = utc_now()
        now_iso = dt_to_iso(now)
        nudge_id = new_ulid()
        payload = {
            "event": "board.activity",
            "board": resolved_slug,
            "latest_seq": 1,
            "unread_for_you": 0,
            "types": ["system"],
            "mentioned": False,
            "nudge_id": nudge_id,
            "sent_at": now_iso,
        }
        res = await self._deliver_http(
            url=sub["url"],
            auth_header_enc=sub["auth_header_enc"],
            hmac_secret_enc=sub["hmac_secret_enc"],
            nudge_id=nudge_id,
            timestamp=int(now.timestamp()),
            payload=payload,
        )
        if res["ok"]:
            with self.services.db.write_tx() as wconn:
                delivery_store.heal_subscription_delivery(
                    wconn,
                    sub_id=sub_id,
                    now_iso=now_iso,
                    force_enable=False,
                )
        return res

    async def _deliver_http(
        self,
        *,
        url: str,
        auth_header_enc: str | None,
        hmac_secret_enc: str,
        nudge_id: str,
        timestamp: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        hmac_secret = self.services.crypto.decrypt(hmac_secret_enc)
        sig = sign_nudge(hmac_secret, timestamp, raw_body)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Board-Nudge-Id": nudge_id,
            "X-Board-Timestamp": str(timestamp),
            "X-Board-Signature": sig,
        }
        if auth_header_enc:
            norm_auth = normalize_auth_header(self.services.crypto.decrypt(auth_header_enc))
            if norm_auth:
                headers["Authorization"] = norm_auth

        try:
            async with self._build_client() as client:
                resp = await client.post(url, content=raw_body, headers=headers)
            if 200 <= resp.status_code < 300:
                return {"ok": True, "status_code": resp.status_code, "error": None}
            return {
                "ok": False,
                "status_code": resp.status_code,
                "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
        except Exception as exc:
            return {"ok": False, "status_code": None, "error": str(exc)[:300]}

    async def process_due_once(self, *, now: datetime | None = None) -> int:
        """Recover stale leases, claim due rows, and send HTTP nudges."""
        current = now or utc_now()
        now_iso = dt_to_iso(current)
        stale_before_iso = dt_to_iso(current - timedelta(seconds=_STALE_LEASE_S))

        with self.services.db.write_tx() as conn:
            delivery_store.recover_stale_sending_outbox(conn, stale_before_iso)
            due_rows = delivery_store.claim_due_outbox_rows(conn, now_iso=now_iso, limit=20)

        processed = 0
        for row in due_rows:
            processed += 1
            if not row["active"]:
                with self.services.db.write_tx() as conn:
                    delivery_store.mark_outbox_suppressed(conn, outbox_id=row["id"], reason="subscription_inactive")
                continue

            rconn = self.services.db.read_conn()
            read_seq = delivery_store.get_cursor(rconn, row["principal_id"], row["board_id"])
            if read_seq >= int(row["latest_seq"]):
                with self.services.db.write_tx() as conn:
                    delivery_store.mark_outbox_suppressed(conn, outbox_id=row["id"], reason="already_acked")
                    if int(row["failure_count"]) > 0:
                        delivery_store.heal_subscription_delivery(
                            conn,
                            sub_id=row["subscription_id"],
                            now_iso=now_iso,
                            force_enable=False,
                        )
                continue

            unread_count, types, mentioned = delivery_store.get_unread_summary_for_nudge(
                rconn,
                board_id=row["board_id"],
                principal_id=row["principal_id"],
                read_seq=read_seq,
            )
            if unread_count == 0:
                with self.services.db.write_tx() as conn:
                    delivery_store.mark_outbox_suppressed(conn, outbox_id=row["id"], reason="zero_unread")
                continue

            payload = {
                "event": "board.activity",
                "board": row["board_slug"],
                "latest_seq": int(row["latest_seq"]),
                "unread_for_you": unread_count,
                "types": types,
                "mentioned": mentioned,
                "nudge_id": row["id"],
                "sent_at": now_iso,
            }
            delivery_res = await self._deliver_http(
                url=row["url"],
                auth_header_enc=row["auth_header_enc"],
                hmac_secret_enc=row["hmac_secret_enc"],
                nudge_id=row["id"],
                timestamp=int(current.timestamp()),
                payload=payload,
            )

            if delivery_res["ok"]:
                with self.services.db.write_tx() as conn:
                    delivery_store.mark_outbox_sent(
                        conn,
                        outbox_id=row["id"],
                        subscription_id=row["subscription_id"],
                        now_iso=utc_now_iso() if now is None else now_iso,
                    )
            else:
                new_attempt = int(row["attempt"]) + 1
                age_s = (current - iso_to_dt(row["created_at"])).total_seconds()
                dead = age_s >= _DEAD_AFTER_S
                delay_s = compute_retry_delay_s(new_attempt)
                next_attempt = dt_to_iso(current + timedelta(seconds=delay_s))
                err_msg = str(delivery_res["error"] or "delivery failed")

                with self.services.db.write_tx() as conn:
                    failures, auto_disabled = delivery_store.record_outbox_failure(
                        conn,
                        outbox_id=row["id"],
                        subscription_id=row["subscription_id"],
                        new_attempt=new_attempt,
                        next_attempt_at=next_attempt,
                        dead=dead,
                        last_error=err_msg,
                        now_iso=now_iso,
                    )
                    if auto_disabled:
                        self._post_auto_disable_alerts(
                            conn,
                            board_slug=row["board_slug"],
                            principal_handle=row["principal_handle"],
                            sub_id=row["subscription_id"],
                            failures=failures,
                            err_msg=err_msg,
                            now_iso=now_iso,
                        )
        return processed

    def _post_auto_disable_alerts(
        self,
        conn: Any,
        *,
        board_slug: str,
        principal_handle: str,
        sub_id: str,
        failures: int,
        err_msg: str,
        now_iso: str,
    ) -> None:
        sys_ctx = system_context()
        alert_body = (
            f"Subscription `{sub_id}` for `@{principal_handle}` was auto-disabled "
            f"after {failures} consecutive nudge delivery failures. Last error: `{err_msg}`"
        )
        target_slugs = [board_slug]
        if board_slug != "ops":
            target_slugs.append("ops")
        for slug in target_slugs:
            board = boards_store.get_board_by_slug(conn, slug)
            if board is None or board["archived_at"] is not None:
                continue
            post_message_in_tx(
                conn,
                services=self.services,
                ctx=sys_ctx,
                board=board,
                thread_title="general",
                msg_type="system",
                body_md=alert_body,
                data={"subscription_id": sub_id, "principal": principal_handle},
                now_iso=now_iso,
            )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Background worker loop polling due outbox rows until stop_event is set."""
        interval = max(0.2, self.services.settings.nudge_poll_interval_s)
        while not stop_event.is_set():
            try:
                await self.process_due_once()
            except Exception:
                logger.exception("Unhandled error in nudge worker loop")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass
