"""SQLite online backup, hardlinked attachment snapshots, retention, and restore drill."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_board.ids import dt_to_iso, iso_to_dt, new_ulid, utc_now
from agent_board.services.attachments import cleanup_orphan_attachments
from agent_board.services.context import AppServices, system_context
from agent_board.services.messages import post_message_in_tx
from agent_board.store import backups as backups_store
from agent_board.store import boards as boards_store
from agent_board.store import delivery as delivery_store
from agent_board.store import idempotency as idemp_store

logger = logging.getLogger(__name__)


def _hardlink_or_copy_tree(src_root: Path, dst_root: Path) -> None:
    if not src_root.exists():
        dst_root.mkdir(parents=True, exist_ok=True)
        return
    for dirpath, _dirnames, filenames in os.walk(src_root):
        rel = Path(dirpath).relative_to(src_root)
        target_dir = dst_root / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            if fname.startswith(".tmp_"):
                continue
            src_file = Path(dirpath) / fname
            dst_file = target_dir / fname
            if dst_file.exists():
                continue
            try:
                os.link(src_file, dst_file)
            except OSError:
                shutil.copy2(src_file, dst_file)


def _prune_retention(backups_dir: Path, *, retain_daily: int, retain_weekly: int) -> None:
    db_files = sorted(backups_dir.glob("board-*.db"), reverse=True)
    parsed_files: list[tuple[Path, str, str]] = []
    for path in db_files:
        stem = path.stem  # board-YYYYMMDD-HHMMSS
        parts = stem.split("-")
        if len(parts) < 2 or len(parts[1]) != 8:
            continue
        day_str = parts[1]
        try:
            dt = datetime.strptime(day_str, "%Y%m%d")
        except ValueError:
            continue
        week_key = f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"
        parsed_files.append((path, day_str, week_key))

    keep: set[Path] = set()
    seen_days: set[str] = set()
    seen_weeks: set[str] = set()
    for path, day_str, week_key in parsed_files:
        if day_str not in seen_days and len(seen_days) < retain_daily:
            seen_days.add(day_str)
            seen_weeks.add(week_key)
            keep.add(path)
        elif week_key not in seen_weeks and len(seen_weeks) < retain_weekly:
            seen_weeks.add(week_key)
            keep.add(path)

    for path, _, _ in parsed_files:
        if path not in keep:
            path.unlink(missing_ok=True)
            ts_suffix = path.stem.removeprefix("board-")
            att_dir = backups_dir / f"attachments-{ts_suffix}"
            if att_dir.exists():
                shutil.rmtree(att_dir, ignore_errors=True)


_BACKUP_LOCK = threading.Lock()


def run_backup(services: AppServices, *, now: datetime | None = None) -> dict[str, Any]:
    """Run online SQLite .backup(), snapshot attachments, apply retention, and log run."""
    with _BACKUP_LOCK:
        current = now or utc_now()
        started_iso = dt_to_iso(current)
        ts_tag = current.strftime("%Y%m%d-%H%M%S")

        backups_dir = services.settings.backups_dir
        backups_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(backups_dir, 0o700)
        db_backup_path = backups_dir / f"board-{ts_tag}.db"
        att_snapshot_path = backups_dir / f"attachments-{ts_tag}"

        ok = True
        detail = "Backup completed"
        size_bytes = 0
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(backups_dir), prefix=".board-backup-", suffix=".tmp")
            os.close(fd)
            os.chmod(tmp_name, 0o600)
            tmp_path = Path(tmp_name)

            src = sqlite3.connect(str(services.settings.db_path), timeout=10.0)
            dst = sqlite3.connect(str(tmp_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, db_backup_path)
            tmp_path = None

            _hardlink_or_copy_tree(services.settings.attachments_dir, att_snapshot_path)
            size_bytes = db_backup_path.stat().st_size if db_backup_path.exists() else 0

            _prune_retention(
                backups_dir,
                retain_daily=services.settings.backup_retain_daily,
                retain_weekly=services.settings.backup_retain_weekly,
            )
            cleanup_orphan_attachments(services, max_age_hours=24)
            cutoff_iso = dt_to_iso(current - timedelta(days=30))
            with services.db.write_tx() as conn:
                idemp_store.prune_idempotency_keys_before(conn, cutoff_iso)
                delivery_store.prune_outbox_before(conn, cutoff_iso)
            services.db.checkpoint_wal()
        except Exception as exc:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            ok = False
            detail = str(exc)[:500]
            logger.exception("Backup failed")

        finished_iso = dt_to_iso(utc_now() if now is None else current)
        backup_id = new_ulid()
        with services.db.write_tx() as conn:
            backups_store.insert_backup_run(
                conn,
                backup_id=backup_id,
                started_at=started_iso,
                finished_at=finished_iso,
                ok=ok,
                db_backup_path=str(db_backup_path) if ok else None,
                size_bytes=size_bytes,
                detail=detail,
            )
        return {
            "id": backup_id,
            "ok": ok,
            "started_at": started_iso,
            "finished_at": finished_iso,
            "db_backup_path": str(db_backup_path) if ok else None,
            "size_bytes": size_bytes,
            "detail": detail,
        }


def check_backup_health(services: AppServices, *, now: datetime | None = None) -> bool:
    """Alert on the ops board if the last successful backup is older than 36 hours."""
    current = now or utc_now()
    latest = backups_store.get_latest_successful_backup(services.db.read_conn())
    if latest is None:
        return True
    age_hours = (current - iso_to_dt(latest["finished_at"])).total_seconds() / 3600.0
    if age_hours <= 36.0:
        return True

    day_key = current.strftime("%Y-%m-%d")
    with services.db.write_tx() as conn:
        ops = boards_store.get_board_by_slug(conn, "ops")
        if ops is not None and ops["archived_at"] is None:
            existing = conn.execute(
                """
                SELECT 1 FROM messages
                WHERE board_id = ? AND type = 'system'
                  AND body_md LIKE ?
                LIMIT 1
                """,
                (ops["id"], f"%backup-alert:{day_key}%"),
            ).fetchone()
            if existing is None:
                post_message_in_tx(
                    conn,
                    services=services,
                    ctx=system_context(),
                    board=ops,
                    thread_title="general",
                    msg_type="system",
                    body_md=(
                        f"Backup warning (`backup-alert:{day_key}`): latest successful backup "
                        f"is {age_hours:.1f} hours old (threshold 36h)."
                    ),
                    data={"age_hours": round(age_hours, 2)},
                    now_iso=dt_to_iso(current),
                )
    return False


def get_backup_status(services: AppServices) -> dict[str, Any]:
    conn = services.db.read_conn()
    latest = backups_store.get_latest_successful_backup(conn)
    runs = backups_store.list_backup_runs(conn, limit=10)
    return {
        "latest": dict(latest) if latest else None,
        "runs": [dict(r) for r in runs],
    }


def run_restore_drill(backup_path: Path) -> dict[str, Any]:
    """Restore backup_path into a temporary directory and verify integrity and row counts."""
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    with tempfile.TemporaryDirectory(prefix="agent-board-restore-") as tmp:
        restored = Path(tmp) / "restored.db"
        src = sqlite3.connect(str(backup_path))
        dst = sqlite3.connect(str(restored))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        conn = sqlite3.connect(str(restored))
        conn.row_factory = sqlite3.Row
        try:
            integrity_rows = conn.execute("PRAGMA integrity_check;").fetchall()
            integrity = [r[0] for r in integrity_rows]
            fk_rows = conn.execute("PRAGMA foreign_key_check;").fetchall()
            counts = {}
            for table in ("principals", "boards", "threads", "messages", "decisions", "issues"):
                row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()
                counts[table] = int(row["cnt"])
        finally:
            conn.close()

    return {
        "backup_path": str(backup_path),
        "integrity_ok": integrity == ["ok"],
        "integrity": integrity,
        "foreign_key_errors": len(fk_rows),
        "counts": counts,
    }


async def run_backup_scheduler(services: AppServices, stop_event: asyncio.Event) -> None:
    """Periodic backup loop running daily and verifying 36h backup freshness."""
    interval = max(60, services.settings.backup_interval_seconds)
    while not stop_event.is_set():
        try:
            latest = backups_store.get_latest_successful_backup(services.db.read_conn())
            now = utc_now()
            should_run = latest is None or ((now - iso_to_dt(latest["finished_at"])).total_seconds() >= interval)
            if should_run:
                await asyncio.to_thread(run_backup, services)
            check_backup_health(services)
        except Exception:
            logger.exception("Unhandled error in backup scheduler")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3600.0)
        except TimeoutError:
            pass
