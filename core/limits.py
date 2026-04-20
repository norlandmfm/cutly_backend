"""Per-user concurrency + rate limiting for Cutly backend.

Pulls caps from Firestore `/config/app_config` (60s in-memory cache) and the
user's `plan` field from `/users/{uid}` (5min cache). Falls back to hard-coded
defaults when Firebase Admin isn't configured or reads fail — the backend
still enforces *some* cap instead of becoming a free-for-all.

Call [reserve] before accepting a job → returns (allowed, code, info).
Call [release] in the job thread's finally → always, even on error.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

# Defaults MUST match lib/models/app_config.dart defaults so behavior is
# consistent when Firestore is unreachable.
_DEFAULTS: Dict[str, int] = {
    "maxConcurrentJobsFree": 1,
    "maxConcurrentJobsPaid": 3,
    "rateLimitJobsPerHourFree": 10,
    "rateLimitJobsPerHourPaid": 60,
}

_CONFIG_TTL_SEC = 60.0
_PLAN_TTL_SEC = 300.0

_config_cache: Dict[str, object] = {"data": None, "ts": 0.0}
_config_lock = threading.Lock()

_user_plan_cache: Dict[str, Tuple[str, float]] = {}
_plan_lock = threading.Lock()


def _read_firestore_config() -> Dict[str, int]:
    """Return merged {defaults + /config/app_config}. Cached 60s. Never raises."""
    now = time.time()
    with _config_lock:
        data = _config_cache.get("data")
        ts = float(_config_cache.get("ts") or 0.0)
        if data is not None and now - ts < _CONFIG_TTL_SEC:
            return dict(data)  # defensive copy

    merged: Dict[str, int] = dict(_DEFAULTS)
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import firestore as fb_firestore  # type: ignore

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        db = fb_firestore.client()
        snap = db.collection("config").document("app_config").get()
        if snap.exists:
            remote = snap.to_dict() or {}
            for key in _DEFAULTS:
                v = remote.get(key)
                if isinstance(v, int) and v > 0:
                    merged[key] = v
    except Exception as e:  # noqa: BLE001 — all errors → defaults + log
        print(f"[limits] Firestore config read failed, using defaults: {e}")

    with _config_lock:
        _config_cache["data"] = merged
        _config_cache["ts"] = time.time()
    return dict(merged)


def _read_user_plan(uid: Optional[str]) -> str:
    """Return 'paid' when user doc has plan == 'paid', else 'free'. Cached 5min."""
    if not uid:
        return "free"
    now = time.time()
    with _plan_lock:
        cached = _user_plan_cache.get(uid)
        if cached and now - cached[1] < _PLAN_TTL_SEC:
            return cached[0]

    plan = "free"
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import firestore as fb_firestore  # type: ignore

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        db = fb_firestore.client()
        snap = db.collection("users").document(uid).get()
        if snap.exists:
            p = (snap.to_dict() or {}).get("plan")
            if p == "paid":
                plan = "paid"
    except Exception as e:  # noqa: BLE001
        print(f"[limits] user plan read failed for {uid}: {e}")

    with _plan_lock:
        _user_plan_cache[uid] = (plan, time.time())
    return plan


def get_limits(uid: Optional[str]) -> Tuple[int, int, str]:
    """Return (max_concurrent, rate_per_hour, plan) for [uid]."""
    cfg = _read_firestore_config()
    plan = _read_user_plan(uid)
    if plan == "paid":
        return int(cfg["maxConcurrentJobsPaid"]), int(cfg["rateLimitJobsPerHourPaid"]), plan
    return int(cfg["maxConcurrentJobsFree"]), int(cfg["rateLimitJobsPerHourFree"]), plan


class UserLimits:
    """Thread-safe per-uid concurrent counter + sliding-window rate tracker.

    Anonymous requests (no uid) share the '_anon_' bucket so they can't
    collectively flood the backend either.
    """

    _concurrent: Dict[str, int] = defaultdict(int)
    _history: Dict[str, Deque[float]] = defaultdict(deque)
    _lock = threading.Lock()

    @classmethod
    def reserve(cls, uid: Optional[str]) -> Tuple[bool, str, Dict[str, object]]:
        """Atomically check both limits and increment the concurrent counter on
        success. Also records the timestamp for rate-limit tracking.

        Returns (allowed, code, info):
          - ('ok', {'concurrent': N, 'used_hour': N, 'plan': 'free'|'paid'})
          - ('rate_limited', {'retry_after': sec, 'limit': N, 'plan': ...})
          - ('too_many', {'limit': N, 'active': N, 'plan': ...})
        """
        key = uid or "_anon_"
        max_concurrent, rate_per_hour, plan = get_limits(uid)
        now = time.time()
        hour_ago = now - 3600.0
        with cls._lock:
            h = cls._history[key]
            while h and h[0] < hour_ago:
                h.popleft()

            if len(h) >= rate_per_hour:
                retry_after = int(max(1, h[0] + 3600.0 - now))
                return False, "rate_limited", {
                    "retry_after": retry_after,
                    "limit": rate_per_hour,
                    "plan": plan,
                }

            if cls._concurrent[key] >= max_concurrent:
                return False, "too_many", {
                    "limit": max_concurrent,
                    "active": cls._concurrent[key],
                    "plan": plan,
                }

            cls._concurrent[key] += 1
            h.append(now)
            return True, "ok", {
                "concurrent": cls._concurrent[key],
                "used_hour": len(h),
                "plan": plan,
            }

    @classmethod
    def release(cls, uid: Optional[str]) -> None:
        """Decrement the concurrent counter. Safe to call multiple times — floored at 0."""
        key = uid or "_anon_"
        with cls._lock:
            if cls._concurrent[key] > 0:
                cls._concurrent[key] -= 1

    @classmethod
    def status(cls, uid: Optional[str]) -> Dict[str, object]:
        """Read-only snapshot for diagnostics (GET /limits/status)."""
        key = uid or "_anon_"
        max_concurrent, rate_per_hour, plan = get_limits(uid)
        now = time.time()
        hour_ago = now - 3600.0
        with cls._lock:
            h = cls._history[key]
            # prune without mutating caller behavior
            used = sum(1 for t in h if t >= hour_ago)
            active = cls._concurrent[key]
        return {
            "uid": uid,
            "plan": plan,
            "concurrent": {"active": active, "limit": max_concurrent},
            "rate": {"used_hour": used, "limit": rate_per_hour},
        }


def invalidate_caches() -> None:
    """Drop config + plan caches. Useful for tests or after admin updates config."""
    with _config_lock:
        _config_cache["data"] = None
        _config_cache["ts"] = 0.0
    with _plan_lock:
        _user_plan_cache.clear()
