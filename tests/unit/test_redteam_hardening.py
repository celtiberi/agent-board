"""Regression tests for v0.7.1 red-team security and reliability hardening."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_board.app import create_app
from agent_board.backup.manager import _prune_retention, run_backup
from agent_board.cli_runtime import install_local_agent_profile
from agent_board.errors import NotFoundError, PermissionDeniedError, ValidationError
from agent_board.ids import utc_now, utc_now_iso
from agent_board.nudger.worker import NudgeWorker
from agent_board.secrets_scan import SecretRedactor
from agent_board.services import attachments as att_svc
from agent_board.services import boards as boards_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import delivery as delivery_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from agent_board.services.auth import authenticate_bearer, create_token, set_membership
from agent_board.telemetry import _parse_transcript_metrics, dispatch_telemetry_alerts


def test_c1_subscription_filter_and_interval_validation(env) -> None:
    with pytest.raises(ValidationError):
        delivery_svc.create_subscription(
            env.services,
            env.bob_ctx,
            url="https://hooks.example.test/overflow",
            boards=["alpha"],
            debounce_s=10**9,
        )
    with pytest.raises(ValidationError):
        delivery_svc.create_subscription(
            env.services,
            env.bob_ctx,
            url="https://hooks.example.test/badtypes",
            boards=["alpha"],
            filter_cfg={"types": "note"},
        )
    with pytest.raises(PermissionDeniedError):
        delivery_svc.create_subscription(
            env.services,
            env.bob_ctx,
            url="https://hooks.example.test/nonmember",
            boards=["ops"],
        )


def test_c2_scoped_server_owner_token_cannot_act_as_server_owner(env) -> None:
    ro_owner_tok = create_token(
        env.services,
        env.owner_ctx,
        principal_handle=env.owner_ctx.handle,
        name="owner-ro",
        board_scope=["*"],
        max_role="readonly",
    )["token"]
    ro_owner_ctx = authenticate_bearer(env.services, ro_owner_tok)
    assert ro_owner_ctx.is_server_owner is True
    assert ro_owner_ctx.has_server_owner_privilege is False

    scoped_owner_tok = create_token(
        env.services,
        env.owner_ctx,
        principal_handle=env.owner_ctx.handle,
        name="owner-alpha",
        board_scope=["alpha"],
        max_role="owner",
    )["token"]
    scoped_owner_ctx = authenticate_bearer(env.services, scoped_owner_tok)
    assert scoped_owner_ctx.has_server_owner_privilege is False

    for restricted_ctx in (ro_owner_ctx, scoped_owner_ctx):
        with pytest.raises(PermissionDeniedError):
            boards_svc.create_board(
                env.services, restricted_ctx, slug="nope", name="Nope", purpose="Should fail"
            )
        with pytest.raises(PermissionDeniedError):
            set_membership(
                env.services, restricted_ctx, board_slug="ops", principal_handle="bob", role="agent"
            )


def test_h2_readonly_token_blocked_from_subscription_mutations_and_test(env) -> None:
    sub = delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://hooks.example.test/bob",
        boards=["alpha"],
    )
    bob_ro_tok = create_token(
        env.services,
        env.owner_ctx,
        principal_handle="bob",
        name="bob-ro",
        board_scope=["*"],
        max_role="readonly",
    )["token"]
    bob_ro_ctx = authenticate_bearer(env.services, bob_ro_tok)

    with pytest.raises(PermissionDeniedError):
        delivery_svc.update_subscription(env.services, bob_ro_ctx, sub_id=sub["id"], debounce_s=5)
    with pytest.raises(PermissionDeniedError):
        delivery_svc.delete_subscription(env.services, bob_ro_ctx, sub_id=sub["id"])
    with pytest.raises(PermissionDeniedError):
        delivery_svc.reenable_subscription(env.services, bob_ro_ctx, sub_id=sub["id"])

    app = create_app(env.services)
    client = TestClient(app)
    resp = client.post(
        f"/api/v1/subscriptions/{sub['id']}/test",
        headers={"Authorization": f"Bearer {bob_ro_tok}"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_h5_outbox_poison_pill_isolated_and_recorded(env) -> None:
    sub = delivery_svc.create_subscription(
        env.services,
        env.bob_ctx,
        url="https://hooks.example.test/poison",
        boards=["alpha"],
    )
    messages_svc.post_message(
        env.services,
        env.dev_ctx,
        board_slug="alpha",
        msg_type="note",
        to=["bob"],
        body_md="@bob wake up",
    )
    # Corrupt hmac_secret_enc directly in DB to simulate a poison-pill row.
    with env.services.db.write_tx() as conn:
        conn.execute(
            "UPDATE subscriptions SET hmac_secret_enc = ? WHERE id = ?",
            ("not-valid-fernet-token", sub["id"]),
        )
        conn.execute("UPDATE outbox SET next_attempt_at = '2000-01-01T00:00:00Z'")

    worker = NudgeWorker(env.services)
    processed = await worker.process_due_once()
    assert processed == 1
    row = env.services.db.read_conn().execute(
        "SELECT attempt, last_error FROM outbox LIMIT 1"
    ).fetchone()
    assert row is not None
    assert int(row["attempt"]) == 1
    assert row["last_error"]


def test_h7_m8_edit_preserves_attachments_and_redact_deletes_and_blocks_decision_issue(env) -> None:
    att = att_svc.upload_attachment(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        filename="proof.txt",
        content_type="text/plain",
        content_bytes=b"hello attachment",
    )
    msg = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="initial body",
        attachment_ids=[att["id"]],
    )
    edited = messages_svc.edit_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        message_id=msg["id"],
        body_md="edited body",
    )
    assert [a["id"] for a in edited["attachments"]] == [att["id"]]

    # Redacting deletes the linked attachment and blocks future download.
    messages_svc.redact_message(
        env.services, env.owner_ctx, board_slug="alpha", message_id=edited["id"], reason="cleanup"
    )
    with pytest.raises(NotFoundError):
        att_svc.get_attachment_for_download(env.services, env.bob_ctx, attachment_id=att["id"])

    # Redacting decision or issue messages is rejected.
    dec = decisions_svc.record_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Use SQLite WAL",
        statement="We use WAL mode for concurrent reads.",
        rationale="Predictable single-file storage.",
        decided_by="bob",
        decided_on="2026-11-24",
    )
    dec_msg = env.services.db.read_conn().execute(
        "SELECT id FROM messages WHERE type = 'decision' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert dec is not None and dec_msg is not None
    with pytest.raises(ValidationError):
        messages_svc.redact_message(
            env.services, env.owner_ctx, board_slug="alpha", message_id=dec_msg["id"], reason="nope"
        )


def test_h8_m18_cli_handle_validation_and_http_validation_headers(env) -> None:
    with pytest.raises(ValueError):
        install_local_agent_profile(
            handle="../evil\n",
            token="ab_test_token",
            url="http://127.0.0.1:8790",
            register_antigravity_mcp=False,
        )

    app = create_app(env.services)
    client = TestClient(app)
    resp = client.post(
        "/api/v1/boards/alpha/messages",
        headers={"Authorization": f"Bearer {env.bob_token}"},
        json={"type": "note", "body_md": "hi", "to": 123},
    )
    assert resp.status_code == 422
    assert resp.headers.get("x-content-type-options") == "nosniff"

    ui_resp = client.get("/login")
    assert ui_resp.status_code == 200
    assert ui_resp.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in ui_resp.headers.get("content-security-policy", "")


def test_h10_m17_backup_permissions_and_retention(env, tmp_path: Path) -> None:
    res = run_backup(env.services)
    assert res["ok"] is True
    db_path = Path(res["db_backup_path"])
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    bdir = tmp_path / "ret"
    bdir.mkdir()
    manual = bdir / "board-manual-keep.db"
    manual.write_bytes(b"manual")
    for day in ("20261124", "20261123", "20261116", "20261109", "20261026"):
        (bdir / f"board-{day}-030000.db").write_bytes(b"snap")

    _prune_retention(bdir, retain_daily=2, retain_weekly=2)
    assert manual.exists()
    remaining = sorted(p.name for p in bdir.glob("board-2026*.db"))
    assert len(remaining) == 3  # 2 daily (Nov 24, Nov 23) + 1 more distinct week (Nov 16)


def test_h11_json_quoted_secrets_redacted() -> None:
    redactor = SecretRedactor()
    sample = '{"password": "supersecretvalue", "api_key": "abcdefghijklmnop123"}'
    scrubbed, changed = redactor.scrub_text(sample)
    assert changed is True
    assert "supersecretvalue" not in scrubbed
    assert "abcdefghijklmnop123" not in scrubbed


def test_m4_m5_m6_trusted_ip_attachment_fts5_and_idempotent_retries(env) -> None:
    # M5: FTS5 reserved keywords never raise sqlite3.OperationalError (500).
    messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        body_md="This note is NOT about OR or AND logic.",
    )
    for query in ("NOT", "AND", "NEAR", "AND*"):
        res = search_svc.search(env.services, env.bob_ctx, board_slug="alpha", query=query)
        assert isinstance(res["hits"], list)
    with pytest.raises(ValidationError):
        search_svc.search(env.services, env.bob_ctx, board_slug="alpha", query="OR")

    # M6: Idempotent retries on edit_message, close_issue, supersede_decision replay cleanly.
    msg = messages_svc.post_message(
        env.services, env.bob_ctx, board_slug="alpha", msg_type="note", body_md="v1"
    )
    e1 = messages_svc.edit_message(
        env.services, env.bob_ctx, board_slug="alpha", message_id=msg["id"], body_md="v2", idempotency_key="edit-k1"
    )
    e2 = messages_svc.edit_message(
        env.services, env.bob_ctx, board_slug="alpha", message_id=msg["id"], body_md="v2", idempotency_key="edit-k1"
    )
    assert e1["id"] == e2["id"]

    issue = issues_svc.open_issue(
        env.services, env.bob_ctx, board_slug="alpha", title="Bug", body_md="Details of bug"
    )
    c1 = issues_svc.close_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        ref=issue["ref"],
        resolution="Fixed in commit abcdef123456 with regression test.",
        idempotency_key="close-k1",
    )
    c2 = issues_svc.close_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        ref=issue["ref"],
        resolution="Fixed in commit abcdef123456 with regression test.",
        idempotency_key="close-k1",
    )
    assert c1["status"] == c2["status"] == "resolved"

    dec = decisions_svc.record_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title="Decision 1",
        statement="Original decision.",
        rationale="Original rationale.",
        decided_by="bob",
        decided_on="2026-11-24",
    )
    s1 = decisions_svc.supersede_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        old_ref=dec["ref"],
        title="Decision 2",
        statement="Updated decision.",
        rationale="Updated rationale.",
        decided_by="bob",
        decided_on="2026-11-24",
        reason="Updated approach",
        idempotency_key="sup-k1",
    )
    s2 = decisions_svc.supersede_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        old_ref=dec["ref"],
        title="Decision 2",
        statement="Updated decision.",
        rationale="Updated rationale.",
        decided_by="bob",
        decided_on="2026-11-24",
        reason="Updated approach",
        idempotency_key="sup-k1",
    )
    assert s1 == s2

    # M4: Web UI attachment download works under trusted_ip auto-login.
    att = att_svc.upload_attachment(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        filename="report.txt",
        content_type="text/plain",
        content_bytes=b"trusted-ip-download-ok",
    )
    env.services.settings.web_trusted_ips = ("127.0.0.1",)
    env.services.settings.web_auto_login_handle = env.owner_ctx.handle
    app = create_app(env.services)
    client = TestClient(app, client=("127.0.0.1", 50000))
    dl = client.get(f"/b/alpha/attachments/{att['id']}", follow_redirects=False)
    assert dl.status_code == 200
    assert dl.content == b"trusted-ip-download-ok"


def test_telemetry_handles_corrupt_jsonl_and_none_hit_rate(tmp_path: Path) -> None:
    log_path = tmp_path / "transcript.jsonl"
    now_iso = utc_now_iso()
    log_path.write_text(
        "\n".join(
            [
                "[]",
                "123",
                '"just-a-string"',
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "created_at": now_iso,
                        "input_tokens": "bad-int",
                        "cache_read_tokens": None,
                        "output_tokens": 42,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    parsed = _parse_transcript_metrics(log_path, now=utc_now())
    assert parsed["stats"]["output_5h"] == 42

    posted_bodies: list[str] = []

    class _FakeResp:
        status_code = 201
        text = "ok"

    class _FakeClient:
        def post(self, path: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResp:
            posted_bodies.append(str(json["body_md"]))
            return _FakeResp()

    due = dispatch_telemetry_alerts(
        {
            "host_id": "test-host",
            "collected_at": now_iso,
            "global": {"uncached_5h": 0, "hit_rate_5h": None},
            "thresholds": {"context_warn_tokens": 150_000},
            "alerts": [{"key": "a:ctx", "severity": "warning", "message": "high ctx"}],
        },
        client=_FakeClient(),
        alert_board="ops",
        alert_to="owner",
        state_path=tmp_path / "state.json",
    )
    assert len(due) == 1
    assert "**—**" in posted_bodies[0]
    assert "None%" not in posted_bodies[0]
