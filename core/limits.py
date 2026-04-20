"""Per-user concurrency + rate limiting for Cutly backend.

Pulls caps from Firestore `/config/app_config` (60s in-memory cache) and the
user's `plan` + `planExpiresAt` fields from `/users/{uid}` (5min cache). Falls
back to hard-coded defaults when Firebase Admin isn't configured or reads fail
— the backend still enforces *some* cap instead of becoming a free-for-all.

Tiered limits (mirrors Dart `PlanLimits.of` + JS `historyCapFor`):
    free / starter / popular / pro / studio
A Firestore config value of **0** on any `maxConcurrentJobs<Tier>` or
`rateLimitJobsPerHour<Tier>` field is the *unlimited* sentinel (typically
Studio). Admin bypass is unconditional.

Call [reserve] before accepting a job → returns (allowed, code, info).
Call [release] in the job thread's finally → always, even on error.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

# Defaults MUST match lib/models/app_config.dart defaults so behavior stays
# consistent when Firestore is unreachable. 0 = unlimited sentinel.
_DEFAULTS: Dict[str, int] = {
    # concurrent jobs per tier
    "maxConcurrentJobsFree": 1,
    "maxConcurrentJobsStarter": 1,
    "maxConcurrentJobsPopular": 2,
    "maxConcurrentJobsPro": 3,
    "maxConcurrentJobsStudio": 5,
    # rate limit (jobs/hour) per tier
    "rateLimitJobsPerHourFree": 5,
    "rateLimitJobsPerHourStarter": 10,
    "rateLimitJobsPerHourPopular": 20,
    "rateLimitJobsPerHourPro": 40,
    "rateLimitJobsPerHourStudio": 100,
    # legacy fallbacks (old admin docs) — read-only here, used only if a
    # tier-specific key is missing from remote. Never written back.
    "maxConcurrentJobsPaid": 3,
    "rateLimitJobsPerHourPaid": 40,
}

_PAID_TIERS = ("starter", "popular", "pro", "studio")

_CONFIG_TTL_SEC = 60.0
_PLAN_TTL_SEC = 300.0
_ADMIN_TTL_SEC = 300.0

# Hardcoded admin email whitelist — MUST match lib/services/auth_service.dart
# `_adminEmails`. Keeping it here (not in Firestore) means a compromised
# Firestore doc cannot grant admin bypass, and it survives Firestore outages.
# Compare lowercase.
_ADMIN_EMAILS = {
    "norlandmfouemo@gmail.com",
}

_config_cache: Dict[str, object] = {"data": None, "ts": 0.0}
_config_lock = threading.Lock()

# Value: (plan, plan_expires_at_epoch_or_None, cached_at)
_user_plan_cache: Dict[str, Tuple[str, Optional[float], float]] = {}
_plan_lock = threading.Lock()

_user_admin_cache: Dict[str, Tuple[bool, float]] = {}
_admin_lock = threading.Lock()


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
                # 0 is a valid sentinel (unlimited) for tiered keys, so keep it.
                if isinstance(v, int) and v >= 0:
                    merged[key] = v
    except Exception as e:  # noqa: BLE001 — all errors → defaults + log
        print(f"[limits] Firestore config read failed, using defaults: {e}")

    with _config_lock:
        _config_cache["data"] = merged
        _config_cache["ts"] = time.time()
    return dict(merged)


def _epoch_from_ts(v) -> Optional[float]:
    """Coerce Firestore Timestamp / datetime / int / str → epoch seconds. None-safe."""
    if v is None:
        return None
    try:
        # firebase_admin returns google.cloud.firestore Timestamp objects that
        # expose `.timestamp()`. Python datetime does too.
        if hasattr(v, "timestamp"):
            return float(v.timestamp())
        if isinstance(v, (int, float)):
            # Heuristic: milliseconds if > 10^12, else seconds.
            return float(v) / 1000.0 if v > 1_000_000_000_000 else float(v)
        if isinstance(v, str):
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None
    return None


def _read_user_plan(uid: Optional[str]) -> str:
    """Return the user's effective plan, applying self-heal for expiry.

    Result is one of: 'free', 'starter', 'popular', 'pro', 'studio'. Unknown
    legacy values (e.g. 'paid') are normalized to 'pro' for backwards-compat,
    since old purchase flows credited a generic paid tier.
    """
    if not uid:
        return "free"
    now = time.time()
    with _plan_lock:
        cached = _user_plan_cache.get(uid)
        if cached and now - cached[2] < _PLAN_TTL_SEC:
            plan, exp, _ = cached
            if plan != "free" and exp is not None and now > exp:
                return "free"
            return plan

    plan = "free"
    expires_at: Optional[float] = None
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import firestore as fb_firestore  # type: ignore

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        db = fb_firestore.client()
        snap = db.collection("users").document(uid).get()
        if snap.exists:
            data = snap.to_dict() or {}
            raw = str(data.get("plan") or "free").strip().lower()
            if raw in ("free", "starter", "popular", "pro", "studio"):
                plan = raw
            elif raw == "paid":
                plan = "pro"  # legacy mapping
            expires_at = _epoch_from_ts(data.get("planExpiresAt"))
    except Exception as e:  # noqa: BLE001
        print(f"[limits] user plan read failed for {uid}: {e}")

    with _plan_lock:
        _user_plan_cache[uid] = (plan, expires_at, time.time())

    # Self-heal: if expired, surface Free so caps apply immediately even
    # before the daily cron sweeps the stale doc.
    if plan != "free" and expires_at is not None and time.time() > expires_at:
        return "free"
    return plan


def _is_admin(uid: Optional[str]) -> bool:
    """Return True when the Firebase Auth email for [uid] is whitelisted.

    Resolution order:
      1. firebase_admin.auth.get_user(uid).email — canonical, un-spoofable.
      2. /users/{uid}.email Firestore field — fallback only if Auth SDK fails.

    Cached 5min keyed by uid. Returns False on any error — fail closed.
    """
    if not uid:
        return False
    now = time.time()
    with _admin_lock:
        cached = _user_admin_cache.get(uid)
        if cached and now - cached[1] < _ADMIN_TTL_SEC:
            return cached[0]

    is_admin = False
    try:
        import firebase_admin  # type: ignore
        from firebase_admin import auth as fb_auth  # type: ignore

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        rec = fb_auth.get_user(uid)
        email = (rec.email or "").strip().lower()
        if email and email in _ADMIN_EMAILS:
            is_admin = True
    except Exception as e:  # noqa: BLE001
        print(f"[limits] admin lookup failed for {uid}: {e}")

    with _admin_lock:
        _user_admin_cache[uid] = (is_admin, time.time())
    return is_admin


def _tier_cap(cfg: Dict[str, int], base_key: str, tier: str) -> int:
    """Resolve cfg[f'{base_key}<Tier>'] with legacy fallback.

    0 is preserved (unlimited sentinel). Unknown tiers fall back to Free.
    """
    tier = (tier or "free").lower()
    tier_key = f"{base_key}{tier.capitalize()}"
    if tier_key in cfg:
        return int(cfg[tier_key])
    # legacy fallback for pre-tier admin docs
    if tier in _PAID_TIERS:
        legacy = f"{base_key}Paid"
        if legacy in cfg:
            return int(cfg[legacy])
    return int(cfg.get(f"{base_key}Free", 1))


def get_limits(uid: Optional[str]) -> Tuple[Optional[int], Optional[int], str]:
    """Return (max_concurrent, rate_per_hour, plan) for [uid].

    Values can be None, meaning *unlimited* — caller must skip the check when
    None. Typically Studio returns (None, None, 'studio').
    """
    cfg = _read_firestore_config()
    plan = _read_user_plan(uid)
    concur = _tier_cap(cfg, "maxConcurrentJobs", plan)
    rate = _tier_cap(cfg, "rateLimitJobsPerHour", plan)
    return (
        None if concur <= 0 else int(concur),
        None if rate <= 0 else int(rate),
        plan,
    )


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
          - (True, 'ok',           {'concurrent': N, 'used_hour': N, 'plan': ...})
          - (False, 'rate_limited', {'retry_after': sec, 'limit': N, 'plan': ...})
          - (False, 'too_many',     {'limit': N, 'active': N, 'plan': ...})

        Admins (email whitelist, resolved via Firebase Auth) bypass both
        checks entirely. Studio tier (unlimited sentinel) also bypasses each
        check it has unlimited for, independently.
        """
        if _is_admin(uid):
            return True, "ok", {"concurrent": 0, "used_hour": 0, "plan": "admin"}
        key = uid or "_anon_"
        max_concurrent, rate_per_hour, plan = get_limits(uid)
        now = time.time()
        hour_ago = now - 3600.0
        with cls._lock:
            h = cls._history[key]
            while h and h[0] < hour_ago:
                h.popleft()

            if rate_per_hour is not None and len(h) >= rate_per_hour:
                retry_after = int(max(1, h[0] + 3600.0 - now))
                return False, "rate_limited", {
                    "retry_after": retry_after,
                    "limit": rate_per_hour,
                    "plan": plan,
                }

            if max_concurrent is not None and cls._concurrent[key] >= max_concurrent:
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
        if _is_admin(uid):
            return {
                "uid": uid,
                "plan": "admin",
                "concurrent": {"active": 0, "limit": None},
                "rate": {"used_hour": 0, "limit": None},
                "bypass": True,
            }
        key = uid or "_anon_"
        max_concurrent, rate_per_hour, plan = get_limits(uid)
        now = time.time()
        hour_ago = now - 3600.0
        with cls._lock:
            h = cls._history[key]
            used = sum(1 for t in h if t >= hour_ago)
            active = cls._concurrent[key]
        return {
            "uid": uid,
            "plan": plan,
            "concurrent": {"active": active, "limit": max_concurrent},
            "rate": {"used_hour": used, "limit": rate_per_hour},
        }


def invalidate_caches() -> None:
    """Drop config + plan + admin caches. Useful for tests or after admin updates config."""
    with _config_lock:
        _config_cache["data"] = None
        _config_cache["ts"] = 0.0
    with _plan_lock:
        _user_plan_cache.clear()
    with _admin_lock:
        _user_admin_cache.clear()
