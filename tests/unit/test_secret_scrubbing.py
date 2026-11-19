"""Unit and integration tests for active server-side secret scrubbing."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

from agent_board.config import load_settings
from agent_board.secrets_scan import REDACTED, SecretRedactor
from agent_board.services import attachments as att_svc
from agent_board.services import boards as boards_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from agent_board.services.context import AppServices
from tests.conftest import BoardTestEnv


def test_token_prefixes_and_audit_prefix_preservation() -> None:
    redactor = SecretRedactor()
    sample = "\n".join(
        [
            "full=ab_BY6CU0kXVaYklua7dae6lGnBHrEtfH6f3VtQMP4dazc",
            "audit_prefix=ab_BY6CU0kX",
            "openai=sk-proj-1234567890ABCDEFGHIJKLMNOP",
            "anthropic=sk-ant-api03-1234567890ABCDEFGHIJ",
            "xai=xai-1234567890ABCDEFGHIJKLMN",
            "google=AIzaSyA123456789012345678901234567890123",
            "gh=ghp_1234567890ABCDEFGHIJKLMNOPQRST",
            "gh_pat=github_pat_1234567890ABCDEFGHIJKLMNOP",
            "aws=AKIAIOSFODNN7EXAMPLE",
            "slack=xoxb-123456789012-1234567890123",
            "webhook=whsec_1234567890ABCDEFGHIJKLMN",
            "fly=FlyV1 fm2_1234567890ABCDEFGHIJKLMN",
            "fly_short=fo1_1234567890ABCDEFGHIJKLMN",
            "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "pem=-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBgkqhkiG9w0\n-----END PRIVATE KEY-----",
            "auth_hdr=Authorization: Bearer my-live-bearer-value-998877",
            "standalone=Bearer standalonetoken1234567890",
        ]
    )
    scrubbed, changed = redactor.scrub_text(sample)
    assert changed is True
    # Audit 11-char prefix is preserved while full ab_ token is scrubbed.
    assert "audit_prefix=ab_BY6CU0kX" in scrubbed
    for secret_fragment in (
        "VaYklua7dae6lGnBHrEtfH6f3VtQMP4dazc",
        "sk-proj-",
        "sk-ant-",
        "xai-12345",
        "AIzaSyA",
        "ghp_1234",
        "github_pat_",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234",
        "whsec_1234",
        "fm2_1234",
        "fo1_1234",
        "eyJhbGciOiJIUzI1NiJ9",
        "MIIBVQIBADANBgkqhkiG9w0",
        "my-live-bearer-value-998877",
        "standalonetoken1234567890",
    ):
        assert secret_fragment not in scrubbed


def test_url_credentials_and_query_params_scrubbed() -> None:
    redactor = SecretRedactor()
    sample = (
        "DB=postgres://appuser:RealDbPass!99@db.example.org:5432/prod?sslmode=require&api_key=realQueryKey8877&page=1"
    )
    scrubbed, changed = redactor.scrub_text(sample)
    assert changed is True
    assert "RealDbPass!99" not in scrubbed
    assert "realQueryKey8877" not in scrubbed
    assert f"postgres://appuser:{REDACTED}@db.example.org:5432/prod" in scrubbed
    assert f"api_key={REDACTED}" in scrubbed
    assert "page=1" in scrubbed
    assert "sslmode=require" in scrubbed

    # Re-scrubbing already redacted URL with a new secret query param still works and stays idempotent.
    second_pass, changed2 = redactor.scrub_text(scrubbed + " token=freshTokenValue12345")
    assert changed2 is True
    assert "freshTokenValue12345" not in second_pass
    third_pass, changed3 = redactor.scrub_text(second_pass)
    assert changed3 is False
    assert third_pass == second_pass


def test_labeled_passwords_vs_safe_placeholders() -> None:
    redactor = SecretRedactor()
    safe_prose = "\n".join(
        [
            "password: vault:proton/bridge",
            "api_key=env:OPENAI_API_KEY",
            "token: <your_token>",
            "secret: $MY_SECRET_VAR",
            "password: expired",
            "password: None",
            "password: ********",
            "api_key: your_key_here_12345",
        ]
    )
    out, changed = redactor.scrub_text(safe_prose)
    assert changed is False
    assert out == safe_prose

    unsafe_prose = "Bridge password: real-bridge-pw-99 and secret=liveSecretValue987654"
    scrubbed, unsafe_changed = redactor.scrub_text(unsafe_prose)
    assert unsafe_changed is True
    assert "real-bridge-pw-99" not in scrubbed
    assert "liveSecretValue987654" not in scrubbed
    assert f"Bridge password: {REDACTED}" in scrubbed
    assert f"secret={REDACTED}" in scrubbed


def test_runtime_env_dotenv_and_settings_secret_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    example_file = tmp_path / ".env.example"
    env_file.write_text(
        'export PROTON_IMAP_PASS="my+Bridge/Secret=99"\nNORMAL_HOST=example.org\n',
        encoding="utf-8",
    )
    example_file.write_text("EXAMPLE_KEY=not-collected-example-secret\n", encoding="utf-8")
    monkeypatch.setenv("CUSTOM_API_SECRET", "envRuntimeValue+42")

    redactor = SecretRedactor.from_environment(
        extra_secrets=["settings-master-secret-key-xyz"],
        env_files=[env_file, example_file],
    )
    sample = " ".join(
        [
            "raw=my+Bridge/Secret=99",
            f"qp={quote_plus('my+Bridge/Secret=99')}",
            f"q={quote('envRuntimeValue+42', safe='')}",
            "cfg=settings-master-secret-key-xyz",
            "ex=not-collected-example-secret",
            "host=example.org",
        ]
    )
    scrubbed, changed = redactor.scrub_text(sample)
    assert changed is True
    assert "my+Bridge/Secret=99" not in scrubbed
    assert quote_plus("my+Bridge/Secret=99") not in scrubbed
    assert quote("envRuntimeValue+42", safe="") not in scrubbed
    assert "settings-master-secret-key-xyz" not in scrubbed
    assert "not-collected-example-secret" in scrubbed
    assert "host=example.org" in scrubbed


def test_scrub_data_masks_sensitive_keys_and_preserves_non_strings() -> None:
    redactor = SecretRedactor()
    payload = {
        "target": "staging",
        "passed": 12,
        "ok": True,
        "password": "plain-pw-value",
        "nested": {
            "api_key": "raw-api-key-value",
            "hmac_secret": "vault:kept",
            "count": 3,
            "note": "has sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        },
        "items": ["Authorization: Bearer abcdefghijklmnopqrstuvwx", 99],
    }
    scrubbed, changed = redactor.scrub_data(payload)
    assert changed is True
    assert scrubbed["passed"] == 12
    assert scrubbed["ok"] is True
    assert scrubbed["password"] == REDACTED
    assert scrubbed["nested"]["api_key"] == REDACTED
    assert scrubbed["nested"]["hmac_secret"] == "vault:kept"
    assert scrubbed["nested"]["count"] == 3
    assert "sk-proj-" not in scrubbed["nested"]["note"]
    assert "abcdefghijklmnopqrstuvwx" not in scrubbed["items"][0]
    assert scrubbed["items"][1] == 99


def test_write_surfaces_and_attachments_scrub_secrets_end_to_end(
    env: BoardTestEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ensure AppServices.create wires redact_env_files + secret_key automatically.
    dotenv = tmp_path / "runtime.env"
    dotenv.write_text("PROTON_PASSWORD=runtimeDotenvSecret77\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_BOARD_REDACT_ENV_FILES", str(dotenv))
    monkeypatch.setenv("AGENT_BOARD_SECRET_KEY", "board-signing-master-key-555")
    settings = load_settings(tmp_path / "srv-data")
    srv = AppServices.create(settings)
    try:
        out, changed = srv.redactor.scrub_text(
            "env=runtimeDotenvSecret77 key=board-signing-master-key-555"
        )
        assert changed is True
        assert out == f"env={REDACTED} key={REDACTED}"
    finally:
        srv.close()
        os.environ.pop("AGENT_BOARD_REDACT_ENV_FILES", None)
        os.environ.pop("AGENT_BOARD_SECRET_KEY", None)

    leaked = "sk-live-secret99887766554433221100"

    # 1. post_message + edit_message
    msg = messages_svc.post_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        msg_type="note",
        thread_title=f"Leak {leaked}",
        body_md=f"Using {leaked}",
        data={"api_key": "raw-structured-secret-value"},
    )
    assert msg["warnings"] == ["secret redacted"]
    assert leaked not in msg["body_md"]
    assert leaked not in msg["thread_title"]
    assert msg["data"]["api_key"] == REDACTED

    edited = messages_svc.edit_message(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        message_id=msg["id"],
        body_md="Updated https://svc:topSecret99@db.example.org/app",
    )
    assert edited["warnings"] == ["secret redacted"]
    assert "topSecret99" not in edited["body_md"]

    # 2. create_thread
    th = boards_svc.create_thread(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title=f"Thread {leaked}",
    )
    assert leaked not in th["title"]

    # 3. record_decision + supersede_decision + revoke_decision
    d1 = decisions_svc.record_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title=f"Dec {leaked}",
        statement=f"Never store {leaked}",
        rationale="Bridge password: decision-pw-1234",
        decided_by="bob",
        decided_on="2026-11-19",
    )
    assert leaked not in d1["title"]
    assert leaked not in d1["statement"]
    assert "decision-pw-1234" not in (d1["rationale"] or "")

    d2 = decisions_svc.supersede_decision(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        old_ref=d1["ref"],
        title=f"Sup {leaked}",
        statement=f"Updated rule {leaked}",
        reason="token=supersedeTokenValue998877",
        decided_by="bob",
        decided_on="2026-11-19",
    )
    assert leaked not in d2["title"]
    assert leaked not in d2["statement"]

    rev = decisions_svc.revoke_decision(
        env.services,
        env.owner_ctx,
        board_slug="alpha",
        ref=d2["ref"],
        reason="secret=revokeSecretValue112233",
    )
    assert rev["status"] == "revoked"

    # 4. open_issue + update_issue + close_issue + reopen_issue
    issue = issues_svc.open_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        title=f"Bug {leaked}",
        body_md=f"Saw {leaked} in logs",
    )
    assert leaked not in issue["title"]
    assert leaked not in issue["body_md"]

    issues_svc.update_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        ref=issue["ref"],
        comment="secret=issueUpdateSecretValue9988",
    )
    closed = issues_svc.close_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        ref=issue["ref"],
        resolution=f"Rotated leaked token {leaked} and fixed sanitizer.",
    )
    assert leaked not in (closed["resolution"] or "")

    issues_svc.reopen_issue(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        ref=issue["ref"],
        reason="password: reopenSecretValue4455",
    )

    # Verify FTS search never indexes any of the leaked secrets.
    for term in (
        "secret99887766554433221100",
        "topSecret99",
        "decision-pw-1234",
        "supersedeTokenValue998877",
        "revokeSecretValue112233",
        "issueUpdateSecretValue9988",
        "reopenSecretValue4455",
    ):
        assert search_svc.search(env.services, env.owner_ctx, query=term)["total"] == 0

    # 5. Text attachments scrubbed on upload; binary attachments untouched.
    att_log = att_svc.upload_attachment(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        filename="trace.log",
        content_type="text/x-log",
        content_bytes=f"line1 OK\nAUTH={leaked}\n".encode(),
    )
    _, log_path = att_svc.get_attachment_for_download(env.services, env.bob_ctx, attachment_id=att_log["id"])
    saved_log = log_path.read_text(encoding="utf-8")
    assert leaked not in saved_log
    assert f"AUTH={REDACTED}" in saved_log

    png_bytes = b"\x89PNG\r\n\x1a\n" + f"fake-{leaked}".encode()
    att_png = att_svc.upload_attachment(
        env.services,
        env.bob_ctx,
        board_slug="alpha",
        filename="shot.png",
        content_type="image/png",
        content_bytes=png_bytes,
    )
    _, png_path = att_svc.get_attachment_for_download(env.services, env.bob_ctx, attachment_id=att_png["id"])
    assert png_path.read_bytes() == png_bytes
