"""Scheduled storage purger for Cutly generated outputs.

Walks ``ROOT/00 OUTPUTS/{job_id}/`` directories and deletes those older than
the retention window configured in Firestore ``/config/app_config``.

Retention is tier-aware: if the job's uid + plan are known via the in-memory
JOBS map or a Firestore lookup, use the matching ``storageRetentionDays<Tier>``.
Otherwise (restart lost the JOBS map, or anonymous job) fall back to
``storageRetentionHoursFree`` — the conservative minimum.

Runs a daemon thread that wakes every [PURGE_INTERVAL_SEC] (default 1h). Safe
to start/stop multiple times (idempotent — kept as a singleton).
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from core.limits import _read_firestore_config, _read_user_plan

PURGE_INTERVAL_SEC = int(os.getenv("CUTLY_PURGE_INTERVAL_SEC", "3600"))  # 1h default

_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()
_stop_flag = threading.Event()


def _plan_retention_seconds(cfg: dict, plan: str) -> float:
    """Translate a plan label into a retention window in seconds.

    'free' → storageRetentionHoursFree hours
    Paid plans → storageRetentionDays<Tier> days. Unknown paid tier defaults
    to the Starter window (most conservative paid tier).
    """
    if plan == "free" or not plan:
        return float(cfg.get("storageRetentionHoursFree", 24)) * 3600.0
    # Tier-specific lookup — the plan string matches the suffix.
    tier_key = "storageRetentionDays" + plan.capitalize()
    days = cfg.get(tier_key)
    if not isinstance(days, int) or days <= 0:
        days = cfg.get("storageRetentionDaysStarter", 7)
    return float(days) * 86400.0


def _resolve_owner_plan(job_id: str, jobs_map: Optional[dict]) -> str:
    """Best-effort owner-plan lookup for [job_id].

    1. Read JOBS[job_id]['uid'] from the live API state (set by the limit
       reserve flow).
    2. If uid found → _read_user_plan from Firestore (cached).
    3. Else → 'free' (conservative: shorter retention).
    """
    uid: Optional[str] = None
    if jobs_map is not None:
        entry = jobs_map.get(job_id)
        if isinstance(entry, dict):
            uid = entry.get("uid")
    if not uid:
        return "free"
    try:
        return _read_user_plan(uid)
    except Exception:
        return "free"


def _purge_once(root: Path, jobs_map: Optional[dict]) -> dict:
    """One pass over ``root / '00 OUTPUTS'``. Returns stats for logging."""
    base = root / "00 OUTPUTS"
    if not base.exists():
        return {"scanned": 0, "deleted": 0, "bytes_freed": 0, "errors": 0}

    cfg = _read_firestore_config()
    now = time.time()
    scanned = deleted = errors = 0
    bytes_freed = 0

    for job_dir in base.iterdir():
        if not job_dir.is_dir():
            continue
        scanned += 1
        try:
            mtime = job_dir.stat().st_mtime
            age = now - mtime
            plan = _resolve_owner_plan(job_dir.name, jobs_map)
            window = _plan_retention_seconds(cfg, plan)
            if age <= window:
                continue
            size = _dir_size(job_dir)
            shutil.rmtree(job_dir, ignore_errors=False)
            deleted += 1
            bytes_freed += size
            print(
                f"[purger] deleted {job_dir.name} "
                f"(age={age/3600:.1f}h window={window/3600:.1f}h "
                f"plan={plan} freed={size/1e6:.1f}MB)"
            )
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[purger] error on {job_dir.name}: {e}")

    return {
        "scanned": scanned,
        "deleted": deleted,
        "bytes_freed": bytes_freed,
        "errors": errors,
    }


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except Exception:
            pass
    return total


def _run_forever(root: Path, jobs_map_provider):
    """Daemon loop — sleeps between passes, bails when _stop_flag is set."""
    # Run once on startup so a freshly-booted server catches up on whatever
    # accumulated during downtime.
    try:
        stats = _purge_once(root, jobs_map_provider())
        print(f"[purger] initial pass: {stats}")
    except Exception as e:  # noqa: BLE001
        print(f"[purger] initial pass failed: {e}")

    while not _stop_flag.is_set():
        if _stop_flag.wait(PURGE_INTERVAL_SEC):
            break
        try:
            stats = _purge_once(root, jobs_map_provider())
            print(f"[purger] hourly pass: {stats}")
        except Exception as e:  # noqa: BLE001
            print(f"[purger] pass failed: {e}")


def start(root: Path, jobs_map_provider) -> None:
    """Start the background purger. Idempotent — second call is a no-op.

    [jobs_map_provider] is a zero-arg callable returning the current JOBS dict
    (passed as a provider so the purger sees the latest map without holding
    a reference to a snapshot).
    """
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_flag.clear()
        _thread = threading.Thread(
            target=_run_forever,
            args=(root, jobs_map_provider),
            daemon=True,
            name="cutly-storage-purger",
        )
        _thread.start()
        print(f"[purger] started (interval={PURGE_INTERVAL_SEC}s, root={root})")


def stop() -> None:
    """Signal the thread to exit at its next wake-up."""
    _stop_flag.set()


def run_manual(root: Path, jobs_map: Optional[dict] = None) -> dict:
    """Manual single pass — exposed via /maintenance/purge for ops/tests."""
    return _purge_once(root, jobs_map)
