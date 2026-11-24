"""Zero-LLM Antigravity transcript token & cache-efficiency telemetry collector."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_board.ids import dt_to_iso, iso_to_dt, utc_now

_RECENT_SCAN_HOURS = 72
_IDLE_TTL_SECONDS = 180.0
_PARTIAL_RETENTION_THRESHOLD = 0.75
_PARTIAL_MIN_UNCACHED = 20_000


@dataclass(frozen=True, slots=True)
class TelemetryThresholds:
    """Deterministic thresholds controlling monitor badges and alert notifications."""

    context_warn_tokens: int = 150_000
    context_critical_tokens: int = 280_000
    agent_uncached_5h: int = 2_500_000
    global_uncached_5h: int = 8_000_000
    unattended_webhook_5h: int = 1_500_000
    unattended_idle_minutes: int = 60
    partial_collapse_count_5h: int = 5
    partial_collapse_uncached_5h: int = 1_000_000


def _load_bound_uuid_to_handle(active_conversations_path: Path) -> dict[str, str]:
    if not active_conversations_path.exists():
        return {}
    try:
        raw = json.loads(active_conversations_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, str] = {}
    for raw_key, entry in raw.items():
        clean_key = str(raw_key).strip().lower()
        key_handle = clean_key.split(":", 1)[1] if ":" in clean_key else "admin"
        if isinstance(entry, str):
            cid = entry.strip()
            handle = key_handle
        elif isinstance(entry, dict):
            cid = str(entry.get("conversation_id", "")).strip()
            handle = str(entry.get("handle", key_handle)).strip().lower() or "admin"
        else:
            continue
        if cid and handle:
            mapping[cid] = handle
    return mapping


def _empty_bucket() -> dict[str, Any]:
    return {
        "uncached_1h": 0,
        "uncached_5h": 0,
        "uncached_24h": 0,
        "cache_read_5h": 0,
        "output_5h": 0,
        "webhook_uncached_5h": 0,
        "user_uncached_5h": 0,
        "hard_drops_5h": {
            "model_switch": 0,
            "idle_ttl": 0,
            "compaction": 0,
            "clean_active": 0,
        },
        "partial_collapses_5h": 0,
        "partial_collapse_uncached_5h": 0,
        "idle_wake_total_5h": 0,
        "idle_wake_partial_5h": 0,
        "idle_wake_zero_5h": 0,
        "steps_5h": 0,
    }


def _add_window_stats(dest: dict[str, Any], src: dict[str, Any]) -> None:
    for key in (
        "uncached_1h",
        "uncached_5h",
        "uncached_24h",
        "cache_read_5h",
        "output_5h",
        "webhook_uncached_5h",
        "user_uncached_5h",
        "partial_collapses_5h",
        "partial_collapse_uncached_5h",
        "idle_wake_total_5h",
        "idle_wake_partial_5h",
        "idle_wake_zero_5h",
        "steps_5h",
    ):
        dest[key] += int(src[key])
    for dk, dv in src["hard_drops_5h"].items():
        dest["hard_drops_5h"][dk] += int(dv)


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    denom = bucket["uncached_5h"] + bucket["cache_read_5h"]
    hit_rate = round((bucket["cache_read_5h"] * 100.0) / denom, 1) if denom > 0 else None
    idle_total = int(bucket["idle_wake_total_5h"])
    idle_misses = int(bucket["idle_wake_partial_5h"]) + int(bucket["idle_wake_zero_5h"])
    idle_miss_rate = round((idle_misses * 100.0) / idle_total, 1) if idle_total > 0 else None
    return {**bucket, "hit_rate_5h": hit_rate, "idle_wake_miss_rate_5h": idle_miss_rate}


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_transcript_metrics(path: Path, *, now: datetime) -> dict[str, Any]:
    """Parse one transcript.jsonl chronologically so resumed turns/gaps stay exact across windows."""
    stats = _empty_bucket()
    cutoff_1h = now - timedelta(hours=1)
    cutoff_5h = now - timedelta(hours=5)
    cutoff_24h = now - timedelta(hours=24)

    planner_count = 0
    prev_ctx = 0
    prev_model: str | None = None
    prev_planner_dt: datetime | None = None
    prev_had_tools = False
    saw_checkpoint = False
    saw_sys_msg = False
    current_trigger = "user"

    live_context_tokens = 0
    latest_model: str | None = None
    latest_step_dt: datetime | None = None
    last_user_dt: datetime | None = None

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            raw_line = line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            ts_raw = obj.get("created_at")
            if not isinstance(ts_raw, str) or not ts_raw:
                continue
            try:
                step_dt = iso_to_dt(ts_raw)
            except Exception:
                continue

            step_type = str(obj.get("type", ""))
            content = obj.get("content") if isinstance(obj.get("content"), str) else ""

            if step_type == "USER_INPUT":
                if content.startswith("Agent Board notification ("):
                    current_trigger = "webhook"
                else:
                    current_trigger = "user"
                    last_user_dt = step_dt
                continue

            if step_type == "SYSTEM_MESSAGE":
                saw_sys_msg = True
                if "Agent Board notification (" in content:
                    current_trigger = "webhook"
                continue

            if step_type == "CHECKPOINT":
                saw_checkpoint = True
                inp = _safe_int(obj.get("input_tokens"))
                cache_r = _safe_int(obj.get("cache_read_tokens"))
                out = _safe_int(obj.get("output_tokens"))
                if step_dt >= cutoff_24h:
                    stats["uncached_24h"] += inp
                if step_dt >= cutoff_5h:
                    stats["uncached_5h"] += inp
                    stats["cache_read_5h"] += cache_r
                    stats["output_5h"] += out
                    if current_trigger == "webhook":
                        stats["webhook_uncached_5h"] += inp
                    elif current_trigger == "user":
                        stats["user_uncached_5h"] += inp
                if step_dt >= cutoff_1h:
                    stats["uncached_1h"] += inp
                continue

            if step_type != "PLANNER_RESPONSE":
                continue

            inp = _safe_int(obj.get("input_tokens"))
            cache_r = _safe_int(obj.get("cache_read_tokens"))
            out = _safe_int(obj.get("output_tokens"))
            model = str(obj.get("model") or "") or None
            has_tools = bool(obj.get("tool_calls"))
            ctx_tokens = inp + cache_r
            gap_s = (step_dt - prev_planner_dt).total_seconds() if prev_planner_dt is not None else 0.0

            planner_count += 1
            live_context_tokens = ctx_tokens
            latest_model = model or latest_model
            latest_step_dt = step_dt

            in_24h = step_dt >= cutoff_24h
            in_5h = step_dt >= cutoff_5h
            in_1h = step_dt >= cutoff_1h

            if in_24h:
                stats["uncached_24h"] += inp
            if in_1h:
                stats["uncached_1h"] += inp
            if in_5h:
                stats["steps_5h"] += 1
                stats["uncached_5h"] += inp
                stats["cache_read_5h"] += cache_r
                stats["output_5h"] += out
                if current_trigger == "webhook":
                    stats["webhook_uncached_5h"] += inp
                elif current_trigger == "user":
                    stats["user_uncached_5h"] += inp

                if planner_count >= 2 and prev_ctx > 0:
                    is_same_model = bool(prev_model and model and prev_model == model)
                    is_partial = (
                        cache_r > 0
                        and not saw_checkpoint
                        and (prev_model is None or model is None or prev_model == model)
                        and gap_s < _IDLE_TTL_SECONDS
                        and cache_r < (_PARTIAL_RETENTION_THRESHOLD * prev_ctx)
                        and inp > _PARTIAL_MIN_UNCACHED
                    )
                    if cache_r == 0:
                        if saw_checkpoint:
                            stats["hard_drops_5h"]["compaction"] += 1
                        elif prev_model and model and model != prev_model:
                            stats["hard_drops_5h"]["model_switch"] += 1
                        elif gap_s >= _IDLE_TTL_SECONDS:
                            stats["hard_drops_5h"]["idle_ttl"] += 1
                        else:
                            stats["hard_drops_5h"]["clean_active"] += 1
                    elif is_partial:
                        stats["partial_collapses_5h"] += 1
                        stats["partial_collapse_uncached_5h"] += inp

                    if (
                        saw_sys_msg
                        and not prev_had_tools
                        and not saw_checkpoint
                        and is_same_model
                        and gap_s < _IDLE_TTL_SECONDS
                    ):
                        stats["idle_wake_total_5h"] += 1
                        if cache_r == 0:
                            stats["idle_wake_zero_5h"] += 1
                        elif is_partial:
                            stats["idle_wake_partial_5h"] += 1

            prev_ctx = ctx_tokens
            if model:
                prev_model = model
            prev_planner_dt = step_dt
            prev_had_tools = has_tools
            saw_checkpoint = False
            saw_sys_msg = False

    return {
        "stats": stats,
        "live_context_tokens": live_context_tokens,
        "latest_model": latest_model,
        "latest_step_at": dt_to_iso(latest_step_dt) if latest_step_dt else None,
        "last_user_input_at": dt_to_iso(last_user_dt) if last_user_dt else None,
    }


def _evaluate_alerts(
    *,
    global_summary: dict[str, Any],
    agents: dict[str, dict[str, Any]],
    now: datetime,
    thresholds: TelemetryThresholds,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if global_summary["uncached_5h"] >= thresholds.global_uncached_5h:
        alerts.append(
            {
                "key": "global:global_uncached_5h",
                "severity": "critical",
                "handle": None,
                "kind": "global_uncached_5h",
                "message": (
                    f"Global 5h uncached burn reached {global_summary['uncached_5h']:,} tokens "
                    f"(threshold {thresholds.global_uncached_5h:,})"
                ),
            }
        )

    for handle in sorted(agents):
        info = agents[handle]
        ctx = int(info["live_context_tokens"])
        if ctx >= thresholds.context_critical_tokens:
            alerts.append(
                {
                    "key": f"{handle}:context_critical",
                    "severity": "critical",
                    "handle": handle,
                    "kind": "context_critical",
                    "message": (
                        f"@{handle} bound chat is at {ctx // 1000:,}k context tokens "
                        f"(>= {thresholds.context_critical_tokens // 1000:,}k; /compact recommended)"
                    ),
                }
            )
        elif ctx >= thresholds.context_warn_tokens:
            alerts.append(
                {
                    "key": f"{handle}:context_warn",
                    "severity": "warning",
                    "handle": handle,
                    "kind": "context_warn",
                    "message": (
                        f"@{handle} bound chat reached {ctx // 1000:,}k context tokens "
                        f"(>= {thresholds.context_warn_tokens // 1000:,}k; consider /compact soon)"
                    ),
                }
            )

        if info["uncached_5h"] >= thresholds.agent_uncached_5h:
            alerts.append(
                {
                    "key": f"{handle}:agent_uncached_5h",
                    "severity": "warning",
                    "handle": handle,
                    "kind": "agent_uncached_5h",
                    "message": (
                        f"@{handle} burned {info['uncached_5h']:,} uncached tokens in 5h "
                        f"(threshold {thresholds.agent_uncached_5h:,})"
                    ),
                }
            )

        last_user_iso = info.get("last_user_input_at")
        idle_mins = (
            (now - iso_to_dt(last_user_iso)).total_seconds() / 60.0 if last_user_iso else float("inf")
        )
        if (
            info["webhook_uncached_5h"] >= thresholds.unattended_webhook_5h
            and idle_mins >= thresholds.unattended_idle_minutes
        ):
            alerts.append(
                {
                    "key": f"{handle}:unattended_webhook_drain",
                    "severity": "critical",
                    "handle": handle,
                    "kind": "unattended_webhook_drain",
                    "message": (
                        f"@{handle} burned {info['webhook_uncached_5h']:,} webhook uncached tokens in 5h "
                        f"with no direct user prompt in >= {thresholds.unattended_idle_minutes}m"
                    ),
                }
            )

        if (
            info["partial_collapses_5h"] >= thresholds.partial_collapse_count_5h
            and info["partial_collapse_uncached_5h"] >= thresholds.partial_collapse_uncached_5h
        ):
            alerts.append(
                {
                    "key": f"{handle}:partial_collapse_spike",
                    "severity": "warning",
                    "handle": handle,
                    "kind": "partial_collapse_spike",
                    "message": (
                        f"@{handle} hit {info['partial_collapses_5h']} active partial cache collapses "
                        f"({info['partial_collapse_uncached_5h']:,} uncached tokens in 5h)"
                    ),
                }
            )
    return alerts


def collect_antigravity_telemetry(
    brain_dir: Path,
    active_conversations_path: Path,
    *,
    host_id: str | None = None,
    now: datetime | None = None,
    thresholds: TelemetryThresholds | None = None,
) -> dict[str, Any]:
    """Scan Antigravity transcript logs and return a sanitized, numbers-only telemetry snapshot."""
    current = now or utc_now()
    cfg = thresholds or TelemetryThresholds()
    resolved_host = (host_id or socket.gethostname().split(".")[0] or "workstation").strip()
    uuid_to_handle = _load_bound_uuid_to_handle(active_conversations_path)

    global_bucket = _empty_bucket()
    agent_buckets: dict[str, dict[str, Any]] = {}
    agent_meta: dict[str, dict[str, Any]] = {}
    sessions_scanned = 0

    if brain_dir.exists():
        cutoff_mtime = (current - timedelta(hours=_RECENT_SCAN_HOURS)).timestamp()
        for entry in sorted(brain_dir.iterdir()):
            if not entry.is_dir():
                continue
            cid = entry.name
            log_path = entry / ".system_generated" / "logs" / "transcript.jsonl"
            if not log_path.is_file():
                continue
            handle = uuid_to_handle.get(cid)
            if handle is None and log_path.stat().st_mtime < cutoff_mtime:
                continue

            parsed = _parse_transcript_metrics(log_path, now=current)
            sessions_scanned += 1
            _add_window_stats(global_bucket, parsed["stats"])

            if handle is not None:
                bucket = agent_buckets.setdefault(handle, _empty_bucket())
                _add_window_stats(bucket, parsed["stats"])
                prev_meta = agent_meta.get(handle)
                step_at = parsed["latest_step_at"] or ""
                if prev_meta is None or step_at >= (prev_meta.get("latest_step_at") or ""):
                    agent_meta[handle] = {
                        "live_context_tokens": parsed["live_context_tokens"],
                        "latest_model": parsed["latest_model"],
                        "latest_step_at": parsed["latest_step_at"],
                        "last_user_input_at": parsed["last_user_input_at"],
                    }

    global_summary = {**_finalize_bucket(global_bucket), "sessions_scanned": sessions_scanned}
    agents_out: dict[str, dict[str, Any]] = {}
    for handle, bucket in sorted(agent_buckets.items()):
        meta = agent_meta[handle]
        agents_out[handle] = {
            **_finalize_bucket(bucket),
            **meta,
        }

    alerts = _evaluate_alerts(
        global_summary=global_summary,
        agents=agents_out,
        now=current,
        thresholds=cfg,
    )
    alerts_by_handle: dict[str, list[dict[str, Any]]] = {}
    for a in alerts:
        h = a.get("handle")
        if h:
            alerts_by_handle.setdefault(str(h), []).append(a)
    for handle, info in agents_out.items():
        info["alerts"] = [
            {"kind": a["kind"], "severity": a["severity"], "message": a["message"]}
            for a in alerts_by_handle.get(handle, [])
        ]

    return {
        "host_id": resolved_host,
        "collected_at": dt_to_iso(current),
        "thresholds": {
            "context_warn_tokens": cfg.context_warn_tokens,
            "context_critical_tokens": cfg.context_critical_tokens,
            "agent_uncached_5h": cfg.agent_uncached_5h,
            "global_uncached_5h": cfg.global_uncached_5h,
            "unattended_webhook_5h": cfg.unattended_webhook_5h,
            "partial_collapse_count_5h": cfg.partial_collapse_count_5h,
            "partial_collapse_uncached_5h": cfg.partial_collapse_uncached_5h,
        },
        "global": global_summary,
        "agents": agents_out,
        "alerts": alerts,
    }


def dispatch_telemetry_alerts(
    snapshot: dict[str, Any],
    *,
    client: Any,
    alert_board: str,
    alert_to: str,
    state_path: Path,
    cooldown_hours: float = 4.0,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Post consolidated threshold breach alerts with per-alert cooldown deduplication."""
    alerts = list(snapshot.get("alerts") or [])
    if not alerts:
        return []

    current = now or utc_now()
    cooldown_s = max(0.0, cooldown_hours * 3600.0)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            last_sent: dict[str, str] = {}
            if state_path.exists():
                try:
                    loaded = json.loads(state_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        last_sent = {str(k): str(v) for k, v in loaded.items()}
                except Exception:
                    last_sent = {}

            due: list[dict[str, Any]] = []
            for a in alerts:
                key = str(a.get("key") or "")
                prev_iso = last_sent.get(key)
                if prev_iso and cooldown_s > 0:
                    try:
                        if (current - iso_to_dt(prev_iso)).total_seconds() < cooldown_s:
                            continue
                    except Exception:
                        pass
                due.append(a)

            if not due:
                return []

            clean_to = alert_to.strip().lstrip("@").lower()
            g = snapshot.get("global", {})
            hit_rate = g.get("hit_rate_5h")
            hit_rate_str = f"{hit_rate}%" if hit_rate is not None else "—"
            lines = [
                f"@{clean_to} Zero-LLM telemetry alert from `{snapshot.get('host_id', 'workstation')}` "
                f"(5h global uncached: **{int(g.get('uncached_5h', 0)):,}**, "
                f"hit rate: **{hit_rate_str}**):",
                "",
            ]
            for a in due:
                lines.append(f"- **[{str(a['severity']).upper()}]** {a['message']}")
            warn_k = int((snapshot.get("thresholds") or {}).get("context_warn_tokens", 150_000)) // 1000
            lines.extend(
                ["", f"Run `/compact` on any oversized tab above {warn_k}k context tokens, or check `/monitor`."]
            )

            resp = client.post(
                f"/boards/{alert_board.strip()}/messages",
                json={
                    "thread_title": "token-telemetry-alerts",
                    "type": "note",
                    "to": [clean_to],
                    "body_md": "\n".join(lines),
                },
                headers={"Idempotency-Key": f"telemetry-{snapshot.get('collected_at', '')}-{len(due)}"},
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Failed posting telemetry alert ({resp.status_code}): {resp.text}")

            now_iso = dt_to_iso(current)
            for a in due:
                last_sent[str(a["key"])] = now_iso
            fd, tmp_name = tempfile.mkstemp(dir=str(state_path.parent), prefix=f".{state_path.name}.", suffix=".tmp")
            try:
                os.chmod(tmp_name, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
                    tmp_fh.write(json.dumps(last_sent, indent=2) + "\n")
                os.replace(tmp_name, state_path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return due
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
