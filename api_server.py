# backend/api_server.py
import os
from dotenv import load_dotenv
load_dotenv()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 8000))
RELOAD = os.getenv("RELOAD", "True").lower() in ("true", "1", "t")

from datetime import time
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
import sys
import io
import uuid
import traceback
import threading
from typing import Dict, List, Optional

# ------------------------------------------------------------------
# bootstrap path
ROOT = Path(__file__).parent.resolve()
sys.path.append(str(ROOT))

# ------------------------------------------------------------------
# core imports (inchangés)
from core.utils import ensure_requirements, safe_filename
from core.item_processor import process_item
from core.paths_and_config import resolve_output_directory
from core.limits import UserLimits
from core import purger

# ------------------------------------------------------------------
# Thread-local logger — fixes concurrent-job UnicodeEncodeError on Windows cp1252 console.
# Multiple job threads all redirect sys.stdout; without this, they race and one thread
# can accidentally print ✅/❌ to the raw cp1252 console → crash.
# Solution: install ONE proxy once; each thread registers its own per-thread buffer.
# ------------------------------------------------------------------
_real_stdout = sys.stdout
_real_stderr = sys.stderr

class _JobLogger:
    """Per-thread stdout/stderr proxy. Threads register their StringIO buffer; others
    fall back to the original console (with UTF-8 safety)."""
    _local = threading.local()

    def set_buffer(self, buf: io.StringIO):
        self._local.buf = buf

    def clear_buffer(self):
        self._local.buf = None

    def _buf(self):
        return getattr(self._local, 'buf', None)

    def write(self, s: str):
        b = self._buf()
        if b is not None:
            b.write(s)
        else:
            try:
                _real_stdout.write(s)
            except UnicodeEncodeError:
                _real_stdout.write(s.encode('utf-8', 'replace').decode('ascii', 'replace'))

    def flush(self):
        b = self._buf()
        if b is not None:
            return
        try:
            _real_stdout.flush()
        except Exception:
            pass

    def isatty(self):
        return False

_job_logger = _JobLogger()
sys.stdout = sys.stderr = _job_logger

# ------------------------------------------------------------------
app = FastAPI(title="Cutly Backend API")

# ------------------------------------------------------------------
# IN-MEMORY JOB STORE (MVP OK)
JOBS: Dict[str, dict] = {}
LOCK = threading.Lock()

# ------------------------------------------------------------------
class SingleJobRequest(BaseModel):
    title: str                          # extract title (or video title if no extraits)
    video_title: Optional[str] = None  # parent video/task title → used for folder + FULL.mp4 name
    url: str
    start: Optional[str] = None
    end: Optional[str] = None
    out_dir: Optional[str] = None
    uid: Optional[str] = None          # Firebase uid — drives per-user limits

class BatchItem(BaseModel):
    title: str
    url: str
    start: Optional[str] = None
    end: Optional[str] = None

class BatchJobRequest(BaseModel):
    title: Optional[str] = None
    items: List[BatchItem]
    uid: Optional[str] = None          # Firebase uid — drives per-user limits


def _raise_limit_error(code: str, info: dict):
    """Map limit-check codes to proper HTTP errors (409/429) with structured detail
    so the Flutter client can branch on error.detail.code."""
    if code == "too_many":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "too_many_concurrent_jobs",
                "limit": info.get("limit"),
                "active": info.get("active"),
                "plan": info.get("plan"),
            },
        )
    if code == "rate_limited":
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "limit": info.get("limit"),
                "retry_after": info.get("retry_after"),
                "plan": info.get("plan"),
            },
        )
    # Should never happen — fail closed.
    raise HTTPException(status_code=500, detail={"code": code, "info": info})

# ------------------------------------------------------------------
@app.get("/ping")
def ping():
    return {"status": "ok"}

def _server_out_dir(job_id: str, video_title: str, extract_title: Optional[str] = None) -> tuple:
    """
    Répertoires de sortie côté serveur — toujours sous ROOT/00 OUTPUTS/{job_id}/.
    N'utilise jamais les chemins fournis par le client.

    Structure créée :
        ROOT/00 OUTPUTS/{job_id}/
            00 inputs/          ← FULL.mp4 téléchargés (YouTube)
            {video_title}/      ← src_dir : SRC link
                {extract_title}/← out_dir : fichiers VIDEO (si extrait distinct)
    """
    safe_vid = safe_filename(video_title)
    safe_ext = safe_filename(extract_title) if (extract_title and extract_title != video_title) else None

    base       = ROOT / "00 OUTPUTS" / job_id
    inputs_dir = base / "00 inputs"
    src_dir    = base / safe_vid
    out_dir    = src_dir / safe_ext if safe_ext else src_dir

    for d in [inputs_dir, src_dir, out_dir]:
        d.mkdir(parents=True, exist_ok=True)

    return out_dir.resolve(), inputs_dir.resolve(), src_dir.resolve()

# ------------------------------------------------------------------
def collect_outputs(out_dir: Path, inputs_dir: Path = None, src_dir: Path = None) -> dict:
    """
    Scanne out_dir pour VIDEO, inputs_dir pour FULL, src_dir pour SRC.
    Les valeurs retournées sont des chemins absolus (str).
    """
    outputs = {"video": None, "src": None, "full": None}

    # VIDEO → dans out_dir (dossier de l'extrait)
    if out_dir.exists():
        for f in out_dir.iterdir():
            if not f.is_file(): continue
            if f.name.lower().endswith("- video.mp4"):
                outputs["video"] = str(f)

    # FULL → dans inputs_dir (00 inputs/)
    if inputs_dir and inputs_dir.exists():
        for f in inputs_dir.iterdir():
            if not f.is_file(): continue
            if f.name.lower().endswith("- full.mp4"):
                outputs["full"] = str(f)

    # SRC → dans src_dir (dossier du titre video), fallback inputs_dir
    for scan in [d for d in [src_dir, inputs_dir] if d and d.exists()]:
        for f in scan.iterdir():
            if not f.is_file(): continue
            if f.name.lower().endswith("- src.mp4"):
                outputs["src"] = str(f)
                break
        if outputs["src"]:
            break

    return outputs

# ------------------------------------------------------------------
# @app.post("/jobs/single")
# def start_single_job(req: SingleJobRequest, bg: BackgroundTasks):
#     job_id = str(uuid.uuid4())[:8]
#     with LOCK:
#         JOBS[job_id] = {
#             "job_id": job_id,
#             "status": "queued",
#             "title": req.title,
#             "outputs": None,
#             "logs": "",
#         }
#     # bg.add_task(run_job_thread, job_id, req)
#     bg.add_task(run_job_thread_limited, job_id, req)
#     return {"job_id": job_id, "status": "queued"}

@app.post("/jobs/single")
def start_single_job(req: SingleJobRequest, bg: BackgroundTasks):
    # Per-user limits must pass BEFORE queueing, so 409/429 surfaces synchronously
    # to the client. release() is called by the worker thread's finally.
    allowed, code, info = UserLimits.reserve(req.uid)
    if not allowed:
        _raise_limit_error(code, info)

    job_id = str(uuid.uuid4())[:8]
    with LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "title": req.title,
            "outputs": None,
            "logs": "",
            "step": "En attente...",
            "error": "",
            "cancelled": False,
            "uid": req.uid,
        }
    bg.add_task(run_job_thread_limited, job_id, req)
    return {"job_id": job_id, "status": "queued", "limits": info}


# ------------------------------------------------------------------
@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

# ------------------------------------------------------------------
@app.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "logs": job.get("logs", ""),
    }

# ------------------------------------------------------------------
@app.get("/jobs/{job_id}/download/{file_type}")
def download_file(job_id: str, file_type: str):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Job not ready")
    outputs = job.get("outputs", {})
    file_path_str = outputs.get(file_type)
    if not file_path_str:
        raise HTTPException(404, "File not found")
    file_path = Path(file_path_str)
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")
    return FileResponse(file_path, filename=file_path.name)

# ------------------------------------------------------------------
# @app.post("/jobs/{job_id}/cancel")
# def cancel_job(job_id: str):
#     with LOCK:
#         job = JOBS.get(job_id)
#         if not job:
#             raise HTTPException(404, "Job not found")
#         if job.get("status") not in ["queued", "running"]:
#             raise HTTPException(400, "Cannot cancel job in this state")
#         job["status"] = "cancelled"
#     # Note: pour réellement arrêter le thread, il faudrait gérer un flag dans run_job_thread
#     return {"job_id": job_id, "status": "cancelled"}

@app.get("/jobs/{job_id}/results/{idx}/download/{file_type}")
def download_batch_result(job_id: str, idx: int, file_type: str):
    """Télécharge un fichier vidéo d'un job batch par index de résultat."""
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Job not ready")
    results = job.get("results", [])
    if idx < 0 or idx >= len(results):
        raise HTTPException(404, "Result index out of range")
    outputs = results[idx].get("outputs", {})
    file_path_str = outputs.get(file_type)
    if not file_path_str:
        raise HTTPException(404, f"File type '{file_type}' not found for result {idx}")
    file_path = Path(file_path_str)
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")
    return FileResponse(file_path, filename=file_path.name)

# ------------------------------------------------------------------
@app.get("/jobs/{job_id}/files")
def list_job_files(job_id: str):
    """Scanne le dossier 00 OUTPUTS/{job_id}/ et retourne tous les fichiers vidéo."""
    job_dir = ROOT / "00 OUTPUTS" / job_id
    if not job_dir.exists():
        raise HTTPException(404, "Job output directory not found")
    files = []
    for f in sorted(job_dir.rglob("*")):
        if not f.is_file():
            continue
        # Inclure les vidéos (.mp4), exclure les FULL (inputs bruts)
        if f.suffix.lower() == ".mp4" and "- full" not in f.name.lower():
            files.append({
                "name": f.name,
                "path": str(f.relative_to(job_dir)),
                "size": f.stat().st_size,
            })
    return {"job_id": job_id, "files": files}

@app.get("/jobs/{job_id}/download_file")
def download_job_file(job_id: str, path: str):
    """Télécharge un fichier spécifique par son chemin relatif dans 00 OUTPUTS/{job_id}/."""
    job_dir = ROOT / "00 OUTPUTS" / job_id
    file_path = (job_dir / path).resolve()
    # Sécurité : vérifier que le fichier est bien dans job_dir
    if not str(file_path).startswith(str(job_dir.resolve())):
        raise HTTPException(403, "Access denied")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(file_path, filename=file_path.name)

# ------------------------------------------------------------------
@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job.get("status") not in ["queued", "running"]:
            raise HTTPException(400, "Cannot cancel job in this state")
        job["cancelled"] = True
        job["status"] = "cancelled"
        job["logs"] += "\n✋ Job cancelled by user."
    return {"job_id": job_id, "status": "cancelled"}


# ------------------------------------------------------------------
import threading
import subprocess
import time

def run_job_thread(job_id: str, req: SingleJobRequest, timeout_sec: int = 300):
    buffer = io.StringIO()
    _job_logger.set_buffer(buffer)   # register per-thread buffer (no global sys.stdout clobber)
    start_time = time.time()

    try:
        # --------------------------------------------------
        # STEP 2.1 — Job démarre
        with LOCK:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["progress"] = 5

        # --------------------------------------------------
        # STEP 2.2 — Requirements (ffmpeg)
        ffmpeg_ok = ensure_requirements()

        with LOCK:
            JOBS[job_id]["progress"] = 10  # préparation terminée

        # --------------------------------------------------
        # Préparation item
        # Treat "00:00:00" end as "no end" (cut to end of file)
        end_str = req.end or ""
        if end_str == "00:00:00":
            end_str = ""

        item = {
            "title": req.title,
            "source": req.url,
            "start": req.start or "00:00:00",
            "end": end_str,
        }

        video_title   = req.video_title or req.title
        extract_title = req.title if req.video_title else None
        out_dir, inputs_dir, src_dir = _server_out_dir(job_id, video_title, extract_title)

        # --------------------------------------------------
        # Step tracker — visible côté Flutter via polling
        def set_step(msg: str):
            with LOCK:
                JOBS[job_id]["step"] = msg
                JOBS[job_id]["logs"] = buffer.getvalue()  # flush logs at each step

        set_step("Préparation...")

        # --------------------------------------------------
        # Wrapper avec check cancel + capture d'exception du thread
        thread_exc: list = [None]

        def process_with_check():
            _job_logger.set_buffer(buffer)   # register for THIS sub-thread
            try:
                process_item(
                    it=item,
                    ffmpeg_ok=ffmpeg_ok,
                    out_dir=out_dir,
                    inputs_dir=inputs_dir,
                    src_dir=src_dir,
                    video_title=video_title,
                    idx=1,
                    cancel_check=lambda: JOBS[job_id]["cancelled"],
                    on_step=set_step,
                )
            except Exception as e:
                thread_exc[0] = e
            finally:
                _job_logger.clear_buffer()   # unregister sub-thread

        # --------------------------------------------------
        # STEP 2.3 — Thread worker
        thread = threading.Thread(target=process_with_check)
        thread.start()

        # --------------------------------------------------
        # STEP 2.4 — Supervision + heartbeat progress
        while thread.is_alive():
            thread.join(timeout=0.5)
            elapsed = time.time() - start_time

            # progress vivant (UI heartbeat, plafonné)
            with LOCK:
                if JOBS[job_id]["progress"] < 90:
                    JOBS[job_id]["progress"] += 1

            # timeout
            if elapsed > timeout_sec:
                with LOCK:
                    JOBS[job_id]["status"] = "timeout"
                    JOBS[job_id]["logs"] += "\n⏱ Job exceeded timeout."
                    JOBS[job_id]["cancelled"] = True
                break

            # cancel utilisateur
            with LOCK:
                if JOBS[job_id]["cancelled"]:
                    JOBS[job_id]["logs"] += "\n✋ Job cancelled by user."
                    break

        # Re-raise any exception that occurred inside the thread
        if thread_exc[0] is not None:
            raise thread_exc[0]

        # --------------------------------------------------
        # STEP 2.5 — Finalisation propre
        outputs = collect_outputs(out_dir, inputs_dir, src_dir)

        # Guard: only require VIDEO file for "done" — FULL/SRC are optional inputs
        has_output = bool(outputs.get("video"))

        with LOCK:
            if JOBS[job_id]["status"] not in ["timeout", "cancelled"]:
                if not has_output:
                    JOBS[job_id].update({
                        "status": "error",
                        "error": f"Aucun fichier VIDEO dans {out_dir}. Vérifie ffmpeg, les timestamps, et les droits d'écriture.",
                        "out_dir": str(out_dir),
                        "inputs_dir": str(inputs_dir),
                        "logs": buffer.getvalue(),
                    })
                else:
                    JOBS[job_id].update({
                        "status": "done",
                        "progress": 100,
                        "out_dir": str(out_dir),
                        "inputs_dir": str(inputs_dir),
                        "outputs": outputs,
                        "logs": buffer.getvalue(),
                    })

    except Exception as e:
        with LOCK:
            JOBS[job_id].update({
                "status": "error",
                "step": "Erreur",
                "error": str(e),
                "trace": traceback.format_exc(),
                "logs": buffer.getvalue(),
            })

    finally:
        _job_logger.clear_buffer()   # unregister main job thread (no global stdout clobber)


# ------------------------------------------------------------------
# MAX_CONCURRENT_JOBS = 3
# active_jobs_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)


MAX_CONCURRENT_JOBS = 3
active_jobs_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

# def run_job_thread_limited(job_id: str, req: SingleJobRequest):
#     # with active_jobs_semaphore:
#     #     run_job_thread(job_id, req)
#     with LOCK:
#         JOBS[job_id]["status"] = "running"
#         JOBS[job_id]["progress"] = 5

def run_job_thread_limited(job_id: str, req: SingleJobRequest):
    try:
        with active_jobs_semaphore:
            run_job_thread(job_id, req)
    finally:
        # Always release the per-user slot, even on crash / semaphore timeout.
        UserLimits.release(req.uid)





# ------------------------------------------------------------------
import json

def log_event(job_id: str, message: str):
    with LOCK:
        job = JOBS[job_id]
        logs = job.get("logs_struct", [])
        logs.append({"time": time.time(), "msg": message})
        job["logs_struct"] = logs



def run_batch_thread(job_id: str, req: BatchJobRequest):
    buffer = io.StringIO()
    _job_logger.set_buffer(buffer)
    try:
        with LOCK:
            JOBS[job_id]["status"] = "running"

        ffmpeg_ok = ensure_requirements()

        for idx, item in enumerate(req.items, start=1):
            if JOBS[job_id]["cancelled"]:
                break
            out_dir, inputs_dir, src_dir = _server_out_dir(job_id, item.title)
            process_item(
                it={
                    "title": item.title,
                    "source": item.url,
                    "start": item.start or "00:00:00",
                    "end": item.end or "",
                },
                ffmpeg_ok=ffmpeg_ok,
                out_dir=out_dir,
                inputs_dir=inputs_dir,
                src_dir=src_dir,
                idx=idx,
                cancel_check=lambda: JOBS[job_id]["cancelled"]
            )
            outputs = collect_outputs(out_dir, inputs_dir, src_dir)
            with LOCK:
                JOBS[job_id]["results"].append({
                    "title": item.title,
                    "out_dir": str(out_dir),
                    "inputs_dir": str(inputs_dir),
                    "outputs": outputs
                })

        with LOCK:
            if JOBS[job_id]["cancelled"]:
                JOBS[job_id]["status"] = "cancelled"
            else:
                JOBS[job_id]["status"] = "done"
            JOBS[job_id]["logs"] = buffer.getvalue()

    except Exception as e:
        with LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
            JOBS[job_id]["trace"] = traceback.format_exc()
            JOBS[job_id]["logs"] = buffer.getvalue()
    finally:
        _job_logger.clear_buffer()


# ------------------------------------------------------------------
@app.post("/jobs/batch")
def start_batch_job(req: BatchJobRequest, bg: BackgroundTasks):
    # Batch counts as a single reservation — one slot, one rate-tick.
    allowed, code, info = UserLimits.reserve(req.uid)
    if not allowed:
        _raise_limit_error(code, info)

    job_id = str(uuid.uuid4())[:8]
    with LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "type": "batch",
            "title": req.title or f"Batch ({len(req.items)} items)",
            "items": len(req.items),
            "results": [],
            "logs": "",
            "cancelled": False,
            "uid": req.uid,
        }
    bg.add_task(_run_batch_thread_limited, job_id, req)
    return {"job_id": job_id, "status": "queued", "limits": info}


def _run_batch_thread_limited(job_id: str, req: BatchJobRequest):
    try:
        run_batch_thread(job_id, req)
    finally:
        UserLimits.release(req.uid)


@app.get("/limits/status")
def get_limits_status(uid: Optional[str] = None):
    """Diagnostics: return current concurrent + rate usage for the given uid.
    Used by the Flutter UI to show a little indicator + by tests."""
    return UserLimits.status(uid)


# ------------------------------------------------------------------
# STORAGE PURGER (G10) — auto-deletes old job outputs
# ------------------------------------------------------------------

@app.on_event("startup")
def _boot_purger():
    """Kick off the background purger once the ASGI app is ready.
    Runs an initial pass so a server restart catches stale dirs from
    previous lives, then every [PURGE_INTERVAL_SEC] (1h by default)."""
    purger.start(ROOT, lambda: JOBS)


@app.on_event("shutdown")
def _stop_purger():
    purger.stop()


@app.post("/maintenance/purge")
def maintenance_purge():
    """Manual trigger — runs one purge pass and returns stats.
    Useful for tests, ops, or a 'force cleanup' admin button."""
    return purger.run_manual(ROOT, JOBS)

# ------------------------------------------------------------------
# STRIPE — CHECKOUT SESSION CREATION
# ------------------------------------------------------------------
# This backend owns ONLY the checkout-session creation flow. The
# post-payment webhook (`checkout.session.completed`) is handled by
# the Firebase Cloud Function `stripeWebhook` (see functions/index.js)
# which offers idempotency via Firestore ledger entries, plan
# duration stacking, and tighter coupling with the user doc. Keeping
# webhook logic out of this process-local backend removes the risk
# of a Raspberry / laptop downtime causing Stripe retries to double-
# credit users.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")

# Checkout redirect URLs — overridable via env so we can point to a
# Firebase Hosting staging URL during beta and swap to the final
# landing domain without touching code. `{CHECKOUT_SESSION_ID}` is
# substituted by Stripe at redirect time.
STRIPE_SUCCESS_URL = os.getenv(
    "STRIPE_SUCCESS_URL",
    "https://cutly.app/payment-success?session_id={CHECKOUT_SESSION_ID}",
)
STRIPE_CANCEL_URL = os.getenv(
    "STRIPE_CANCEL_URL",
    "https://cutly.app/payment-cancel",
)

# Credit packs: id → (credits, price_cents).
# MUST stay in sync with packs_for() / AppConfig defaults on the
# Flutter side + resolvePack() in functions/index.js. The Cloud
# Function webhook recomputes credits from /config/app_config, so
# this dict is only authoritative for pricing + Stripe line items.
CREDIT_PACKS = {
    "sos":     (10,    149),    # 1.49 EUR
    "starter": (100,   499),    # 4.99 EUR
    "popular": (300,   999),    # 9.99 EUR
    "pro":     (1000,  2499),   # 24.99 EUR
    "studio":  (2500,  4999),   # 49.99 EUR — V2/admin-gated on the client
}

class CheckoutRequest(BaseModel):
    pack_id: str
    uid: str

@app.post("/payments/create-checkout")
def create_checkout(req: CheckoutRequest):
    """Create a Stripe Checkout session for a credit pack.

    The session embeds the Firebase UID as both `client_reference_id`
    (Stripe-idiomatic) and a `metadata.uid` fallback so the Cloud
    Function webhook can always resolve the buyer. `metadata.pack_id`
    is the single source of truth the webhook uses to credit the
    correct pack — the legacy `credits` field is intentionally not
    sent to avoid drift when the admin edits /config/app_config.
    """
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe not configured (set STRIPE_SECRET_KEY in .env)")

    pack = CREDIT_PACKS.get(req.pack_id)
    if not pack:
        raise HTTPException(400, f"Unknown pack: {req.pack_id}")

    credits, price_cents = pack

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            client_reference_id=req.uid,
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": price_cents,
                    "product_data": {
                        "name": f"Cutly — {credits} credits ({req.pack_id})",
                    },
                },
                "quantity": 1,
            }],
            metadata={
                "uid": req.uid,
                "pack_id": req.pack_id,
            },
            success_url=STRIPE_SUCCESS_URL,
            cancel_url=STRIPE_CANCEL_URL,
        )
        return {"checkout_url": session.url}

    except ImportError:
        raise HTTPException(503, "stripe package not installed (pip install stripe)")
    except Exception as e:
        raise HTTPException(500, f"Stripe error: {e}")


# ------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host=HOST, port=PORT, reload=RELOAD)
