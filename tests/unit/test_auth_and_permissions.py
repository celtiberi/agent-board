"""Unit tests for token scopes, role matrix, expiry/revocation, login lockout, and rate limits."""

from __future__ import annotations

import pyotp
import pytest

from agent_board.errors import AuthError, PermissionDeniedError, RateLimitError
from agent_board.services import boards as boards_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import messages as messages_svc
from agent_board.services import search as search_svc
from agent_board.services.auth import (
    RateLimiter,
    authenticate_bearer,
    create_principal,
    create_token,
    login_human,
    remove_membership,
    revoke_token,
    set_human_password,
    set_principal_disabled,
)
from tests.conftest import BoardTestEnv


@pytest.mark.parametrize(
    ("role_name", "can_read", "can_post", "can_redact"),
    [
        ("owner", True, True, True),
        ("agent", True, True, False),
        ("readonly", True, False, False),
    ],
)
def test_role_matrix(env: BoardTestEnv, role_name: str, can_read: bool, can_post: bool, can_redact: bool) -> None:
    seed_msg = messages_svc.post_message(
        env.services, env.owner_ctx, board_slug="alpha", msg_type="note", body_md="Seed msg"
    )
    ctx_map = {
        "owner": env.owner_ctx,
        "agent": env.bob_ctx,
        "readonly": env.ro_ctx,
    }
    ctx = ctx_map[role_name]

    if can_read:
        assert messages_svc.read_messages(env.services, ctx, board_slug="alpha", since_seq=0)

    if can_post:
        posted = messages_svc.post_message(
            env.services, ctx, board_slug="alpha", msg_type="note", body_md=f"From {role_name}"
        )
        assert posted["seq"] >= 2
    else:
        with pytest.raises(PermissionDeniedError):
            messages_svc.post_message(env.services, ctx, board_slug="alpha", msg_type="note", body_md="Blocked")

    if can_redact:
        res = messages_svc.redact_message(
            env.services, ctx, board_slug="alpha", message_id=seed_msg["id"], reason="test"
        )
        assert res["redacted_at"] is not None
    else:
        with pytest.raises(PermissionDeniedError):
            messages_svc.redact_message(env.services, ctx, board_slug="alpha", message_id=seed_msg["id"], reason="test")


def test_token_scope_capping_revocation_expiry_and_disabled_principal(env: BoardTestEnv) -> None:
    boards_svc.create_board(env.services, env.owner_ctx, slug="beta", name="Beta", purpose="Second board")
    # Bob has agent membership on alpha, none on beta; issue scoped token to alpha with max_role=readonly
    scoped = create_token(
        env.services,
        env.owner_ctx,
        principal_handle="bob",
        name="bob-alpha-ro",
        board_scope=["alpha"],
        max_role="readonly",
    )
    scoped_ctx = authenticate_bearer(env.services, scoped["token"])
    assert scoped_ctx.allows_board("alpha") is True
    assert scoped_ctx.allows_board("beta") is False

    # Effective role is min(agent, readonly) == readonly
    with pytest.raises(PermissionDeniedError):
        messages_svc.post_message(env.services, scoped_ctx, board_slug="alpha", msg_type="note", body_md="Denied")
    with pytest.raises(PermissionDeniedError):
        boards_svc.get_board(env.services, scoped_ctx, board_slug="beta")

    # Revoke token
    revoke_token(env.services, env.owner_ctx, token_id=scoped["id"])
    with pytest.raises(AuthError):
        authenticate_bearer(env.services, scoped["token"])

    # Expired token
    expired = create_token(
        env.services,
        env.owner_ctx,
        principal_handle="bob",
        name="bob-expired",
        board_scope=["*"],
        max_role="agent",
        expires_at="2020-01-01T00:00:00Z",
    )
    with pytest.raises(AuthError):
        authenticate_bearer(env.services, expired["token"])

    # Disabled principal rejects existing active token
    set_principal_disabled(env.services, env.owner_ctx, handle="bob", disabled=True)
    with pytest.raises(AuthError):
        authenticate_bearer(env.services, env.bob_token)


def test_human_login_totp_and_lockout(env: BoardTestEnv) -> None:
    secret = pyotp.random_base32()
    set_human_password(env.services, handle="owner", password="correct-horse-battery", totp_secret=secret)

    # 5 bad passwords lock out account
    for _ in range(5):
        with pytest.raises(AuthError):
            login_human(env.services, handle="owner", password="wrong")

    with pytest.raises(RateLimitError):
        login_human(env.services, handle="owner", password="correct-horse-battery")

    # Reset via password change and verify TOTP check
    set_human_password(env.services, handle="owner", password="correct-horse-battery", totp_secret=secret)
    with pytest.raises(AuthError):
        login_human(env.services, handle="owner", password="correct-horse-battery", totp_code="000000")

    code = pyotp.TOTP(secret).now()
    session_tok, ctx = login_human(env.services, handle="owner", password="correct-horse-battery", totp_code=code)
    assert session_tok
    assert ctx.handle == "owner"


def test_rate_limiter_enforces_write_and_read_caps() -> None:
    limiter = RateLimiter(requests_per_min=5, writes_per_min=2)
    limiter.check("t1", is_write=True)
    limiter.check("t1", is_write=True)
    with pytest.raises(RateLimitError):
        limiter.check("t1", is_write=True)
    limiter.check("t1", is_write=False)
    limiter.check("t1", is_write=False)
    limiter.check("t1", is_write=False)
    with pytest.raises(RateLimitError):
        limiter.check("t1", is_write=False)
    # Suppress unused import check
    assert decisions_svc is not None


def test_server_owner_agent_token_sees_and_manages_all_boards(env: BoardTestEnv) -> None:
    create_principal(
        env.services,
        env.owner_ctx,
        handle="manager-ai",
        kind="agent",
        display_name="Manager AI",
        is_server_owner=True,
    )
    boards_svc.create_board(env.services, env.owner_ctx, slug="gamma", name="Gamma", purpose="Third board")
    remove_membership(env.services, env.owner_ctx, board_slug="gamma", principal_handle="manager-ai")

    mgr_tok = create_token(
        env.services,
        env.owner_ctx,
        principal_handle="manager-ai",
        name="mgr-all",
        board_scope=["*"],
        max_role="owner",
    )
    mgr_ctx = authenticate_bearer(env.services, mgr_tok["token"])

    visible = {b["slug"]: b["role"] for b in boards_svc.list_boards(env.services, mgr_ctx)}
    assert visible["alpha"] == "owner"
    assert visible["gamma"] == "owner"

    posted = messages_svc.post_message(
        env.services, mgr_ctx, board_slug="gamma", msg_type="note", body_md="Owner token visibility check"
    )
    assert posted["seq"] == 1

    hits = search_svc.search(env.services, mgr_ctx, query="visibility")
    assert any(h["board_slug"] == "gamma" for h in hits["hits"])

    restricted_tok = create_token(
        env.services,
        env.owner_ctx,
        principal_handle="manager-ai",
        name="mgr-alpha-ro",
        board_scope=["alpha"],
        max_role="readonly",
    )
    restricted_ctx = authenticate_bearer(env.services, restricted_tok["token"])
    restricted_visible = {b["slug"]: b["role"] for b in boards_svc.list_boards(env.services, restricted_ctx)}
    assert list(restricted_visible.keys()) == ["alpha"]
    assert restricted_visible["alpha"] == "readonly"
    with pytest.raises(PermissionDeniedError):
        messages_svc.post_message(env.services, restricted_ctx, board_slug="alpha", msg_type="note", body_md="Denied")


def test_local_agent_profile_installer_and_loader(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from pathlib import Path

    from agent_board.board_cli import _load_config, install_local_agent_profile

    fake_home = Path(str(tmp_path))
    monkeypatch.setenv("HOME", str(fake_home))

    info = install_local_agent_profile(
        "research-agent",
        "http://127.0.0.1:8790",
        "ab_test_research_token_123",
        register_antigravity_mcp=True,
    )
    profile_path = fake_home / ".config" / "agent-board" / "profiles" / "research-agent.toml"
    wrapper_path = fake_home / ".local" / "bin" / "board-research-agent"
    mcp_path = fake_home / ".gemini" / "config" / "mcp_config.json"

    assert info["profile_path"] == str(profile_path)
    assert profile_path.exists()
    assert wrapper_path.exists()
    assert mcp_path.exists()

    mcp_json = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert mcp_json["mcpServers"]["agent-board-research-agent"]["args"] == [
        "--profile",
        "research-agent",
        "mcp-stdio",
    ]

    url, tok = _load_config(profile_override="research-agent")
    assert url == "http://127.0.0.1:8790"
    assert tok == "ab_test_research_token_123"
