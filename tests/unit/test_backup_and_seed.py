"""Unit tests for declarative seeding, demo workspace, online backups, >36h alerts, and restore drill."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from agent_board.backup.manager import check_backup_health, run_backup, run_restore_drill
from agent_board.config import load_settings
from agent_board.ids import dt_to_iso, utc_now
from agent_board.services import boards as boards_svc
from agent_board.services import decisions as decisions_svc
from agent_board.services import issues as issues_svc
from agent_board.services import messages as messages_svc
from agent_board.services.context import AppServices, AuthContext
from agent_board.services.seed import seed_demo_workspace, seed_from_spec
from agent_board.store import principals as principals_store


def test_seed_demo_and_backup_restore_drill(tmp_path: Path) -> None:
    settings = load_settings(data_dir=tmp_path / "data")
    services = AppServices.create(settings)
    try:
        seed_res = seed_demo_workspace(services)
        assert seed_res["boards"] == ["ops", "example-app"]
        # Idempotent second call
        seed_demo_workspace(services)

        # Custom declarative seed
        custom = seed_from_spec(
            services,
            {
                "principals": [{"handle": "ci-bot", "kind": "agent", "display_name": "CI Bot"}],
                "boards": [
                    {
                        "slug": "custom-proj",
                        "name": "Custom Project",
                        "purpose": "Custom seeded board.",
                        "members": [{"handle": "ci-bot", "role": "readonly"}],
                        "threads": ["releases"],
                    }
                ],
            },
        )
        assert "custom-proj" in custom["boards"]

        owner_row = principals_store.get_first_server_owner(services.db.read_conn())
        assert owner_row is not None
        owner_ctx = AuthContext(
            principal_id=owner_row["id"],
            handle=owner_row["handle"],
            kind="human",
            display_name=owner_row["display_name"],
            is_server_owner=True,
            auth_kind="session",
            board_scope=("*",),
            max_role="owner",
        )

        visible = {b["slug"] for b in boards_svc.list_boards(services, owner_ctx)}
        assert {"ops", "example-app", "custom-proj"} <= visible

        decs = decisions_svc.list_decisions(services, owner_ctx, board_slug="example-app")
        assert [d["ref"] for d in decs] == ["D-1", "D-2"]

        iss = issues_svc.list_issues(services, owner_ctx, board_slug="example-app")
        assert [i["ref"] for i in iss] == ["I-1"]

        b_res = run_backup(services)
        assert b_res["ok"] is True
        backup_path = Path(b_res["db_backup_path"])
        assert backup_path.exists()

        drill = run_restore_drill(backup_path)
        assert drill["integrity_ok"] is True
        assert drill["foreign_key_errors"] == 0
        assert drill["counts"]["boards"] == 3
        assert drill["counts"]["decisions"] == 2
        assert drill["counts"]["issues"] == 1

        # Simulate stale backup (>36h) -> posts system alert on ops once per day
        stale_ts = dt_to_iso(utc_now() - timedelta(hours=40))
        with services.db.write_tx() as conn:
            conn.execute("UPDATE backup_runs SET finished_at = ?", (stale_ts,))
        assert check_backup_health(services) is False
        assert check_backup_health(services) is False

        ops_msgs = messages_svc.read_messages(services, owner_ctx, board_slug="ops", since_seq=0)
        backup_alerts = [m for m in ops_msgs["messages"] if "backup-alert:" in m["body_md"]]
        assert len(backup_alerts) == 1
    finally:
        services.close()


def test_backup_prunes_old_outbox_rows_and_keeps_recent(tmp_path: Path) -> None:
    from agent_board.services import delivery as delivery_svc
    from agent_board.store import delivery as delivery_store

    settings = load_settings(data_dir=tmp_path / "data")
    services = AppServices.create(settings)
    try:
        seed_demo_workspace(services)
        owner_row = principals_store.get_first_server_owner(services.db.read_conn())
        assert owner_row is not None
        owner_ctx = AuthContext(
            principal_id=owner_row["id"],
            handle=owner_row["handle"],
            kind="human",
            display_name=owner_row["display_name"],
            is_server_owner=True,
            auth_kind="session",
            board_scope=("*",),
            max_role="owner",
        )
        sub = delivery_svc.create_subscription(
            services, owner_ctx, url="https://example.invalid/hook", boards=["ops"]
        )
        ops = boards_svc.get_board(services, owner_ctx, board_slug="ops")
        now = utc_now()
        old_ts = dt_to_iso(now - timedelta(days=45))
        recent_ts = dt_to_iso(now - timedelta(days=2))

        with services.db.write_tx() as conn:
            for idx, (status, ts) in enumerate(
                [
                    ("sent", old_ts),
                    ("suppressed", old_ts),
                    ("dead", old_ts),
                    ("sent", recent_ts),
                    ("pending", old_ts),
                ],
                start=1,
            ):
                delivery_store.enqueue_outbox_pending(
                    conn,
                    outbox_id=f"01TESTPRUNEOUTBOX00000000{idx}",
                    subscription_id=sub["id"],
                    board_id=ops["id"],
                    latest_seq=idx,
                    next_attempt_at=ts,
                    created_at=ts,
                )
                if status != "pending":
                    conn.execute(
                        "UPDATE outbox SET status = ?, created_at = ? WHERE id = ?",
                        (status, ts, f"01TESTPRUNEOUTBOX00000000{idx}"),
                    )

        assert services.db.read_conn().execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 5

        res = run_backup(services, now=now)
        assert res["ok"] is True
        remaining = {
            r["id"] for r in services.db.read_conn().execute("SELECT id FROM outbox").fetchall()
        }
        assert remaining == {
            "01TESTPRUNEOUTBOX000000004",  # recent sent preserved
            "01TESTPRUNEOUTBOX000000005",  # old pending preserved
        }
    finally:
        services.close()
