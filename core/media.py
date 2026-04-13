# core\media.py
import re
import os
import sys
import ctypes
import shutil
import threading
from core.utils import run
from pathlib import Path
from subprocess import run
from core.sources import ytinfo_json

# ── Download lock: one lock per output file path to prevent concurrent yt-dlp ──
_dl_locks: dict = {}
_dl_locks_mutex = threading.Lock()

def _get_dl_lock(path: str) -> threading.Lock:
    with _dl_locks_mutex:
        if path not in _dl_locks:
            _dl_locks[path] = threading.Lock()
        return _dl_locks[path]



# ======================================================================
# ---------------- Téléchargement + Découpe
# URL Cleanup: Removes timestamps &t=xx
# Video ID: Tries original_video_id first, then id
# Downloader: yt-dlp with optional aria2c for speed
# Extractor args: Android client to bypass restrictions
# Resume / safety: Adds --continue, --no-overwrites, --no-part
# Caching logic: Works with cleaned URL
# => Robust, fast and safe, especially for repeated or large downloads, and avoids redundant downloads caused by URL query parameters.
# Even cleaner: “bulletproof” version: handles: redirects, cache collisions, partial downloads, and optional aria2c all in one. "Production-ready" for video caching.
# ======================================================================
# def download_to_cache(video_url: str, cache_dir: Path, cache_map: dict, min_size: int = 3_000_000, max_retries: int = 2):
def download_to_cache(video_url: str, cache_dir: Path, cache_map: dict, target_file: Path = None, min_size: int = 3_000_000, max_retries: int = 2):
    """
    Download a video once and cache it locally.

    Parameters:
        video_url (str): Original video URL.
        cache_dir (Path): Directory to store cached videos.
        cache_map (dict): Maps video URLs to cached file paths.
        min_size (int): Minimum valid file size in bytes.
        max_retries (int): Number of download retries if failed.

    Returns:
        Path or None: Cached file path if successful, else None.
    """

    # ------------------------
    # 0. Vérifie cache global. Éviter de re-télécharger la même vidéo
    # ------------------------
    if video_url in cache_map:
        return cache_map[video_url]

    # ------------------------
    # 1. Clean URL. On enlève &t=xx
    # ------------------------
    clean_url = re.sub(r"[&?]t=\d+s", "", video_url)

    # ------------------------
    # 2. Get video info
    # ------------------------
    real_url = clean_url

    # Fallback spécial pour les clips
    if "youtube.com/clip/" in clean_url:
        orig_info = ytinfo_json(clean_url)
        if orig_info:
            real_url = orig_info.get("original_url") or orig_info.get("webpage_url") or clean_url

    info = ytinfo_json(clean_url)
    if not info:
        print(f"❌ Could not retrieve video info: {video_url}")
        return None

    # ID vidéo
    vid = info.get("original_video_id") or info.get("id")
    if not vid:
        print(f"❌ Could not determine video ID: {video_url}")
        return None

    # ------------------------
    # 3. Prepare cache file
    # ------------------------
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_file if target_file else cache_dir / f"{vid}.mp4"

    # ------------------------
    # 4. Return if already cached (quick check before acquiring lock)
    # ------------------------
    if out_file.exists() and out_file.stat().st_size > min_size:
        cache_map[video_url] = out_file
        return out_file

    # ------------------------
    # 4b. Acquire per-file lock — prevents two concurrent yt-dlp processes
    #     writing to / merging the same output file (→ avoids [Errno 22])
    # ------------------------
    dl_lock = _get_dl_lock(str(out_file))
    with dl_lock:
        # Re-check inside the lock (another thread may have just finished)
        if out_file.exists() and out_file.stat().st_size > min_size:
            print(f"ℹ️  Déjà téléchargé par un autre thread : {out_file.name}")
            cache_map[video_url] = out_file
            return out_file

        print(f"⬇️  Téléchargement source: {real_url}")

        # ------------------------
        # 5. Build download command — aria2c si dispo, sinon fallback
        # ------------------------
        use_aria2 = shutil.which("aria2c") is not None

        base_cmd = [
            "yt-dlp",
            "--extractor-args", "youtube:player_client=android",
            "--continue", "--no-overwrites", "--no-part",
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", str(out_file),
            real_url
        ]

        if use_aria2:
            base_cmd += [
                "--downloader", "aria2c",
                "--downloader-args", "aria2c:--max-connection-per-server=16 --split=16 --min-split-size=1M"
            ]

        # ------------------------
        # 6. Download with retries
        # ------------------------
        for attempt in range(1, max_retries + 1):
            print(f"⬇️  Tentative {attempt}/{max_retries} : {real_url}")
            result = run(base_cmd, capture_output=True, text=True)

            # Print yt-dlp output so it ends up in job logs
            if result and result.stdout:
                for line in result.stdout.splitlines():
                    if line.strip() and not line.startswith('\r'):
                        print(f"[yt-dlp] {line}")
            if result and result.stderr:
                for line in result.stderr.splitlines():
                    if line.strip():
                        print(f"[yt-dlp] {line}")

            if out_file.exists() and out_file.stat().st_size > min_size:
                cache_map[video_url] = out_file
                print(f"✅ Téléchargement OK : {out_file.name}")
                return out_file
            else:
                size = out_file.stat().st_size if out_file.exists() else 0
                print(f"⚠️  Tentative {attempt} échouée (taille={size} bytes)")

        # ------------------------
        # 7. Failure
        # ------------------------
        print(f"❌ Téléchargement échoué après {max_retries} tentatives : {video_url}")
        return None


# ======================================================================
# ---------------- CUTTER FROM CUT-ONLY
# ✔ FAST: Because -ss is placed before -i, FFmpeg performs a fast seek to the start region
# ✔ ACCURATE: Because reencoding, FFmpeg is allowed to cut between keyframes with perfect precision. No keyframe restriction.
# ✔ REENCODE: Because of use: -c:v libx264 & -c:a aac & -c:v libx264 & -preset veryfast & -c:a aac -b:a 192k
# ✔ GOOD OUTPUT:
#       -vf scale=1280:-1  (→ Ensures vertical resizing with preserved aspect ratio. Nice for YouTube Shorts or consistent size.)
#       -movflags +faststart (→ Perfect for web playback (“Moov atom” moved to start), improves loading on YouTube)
# ======================================================================
def cut(ffmpeg_ok, src_path, start, end, out_path: Path, accurate=False): # ou "def cut_segment"
    if not ffmpeg_ok:
        print(f"⏭️  ffmpeg absent → pas de découpe ({out_path.name})")
        return False
    
    print(f"✂️  Cutting: {start} → {end} -> {out_path.name} ...")
    
    # # Fast but not Accurate & no Reencoding
    # if accurate:
    #     run([
    #         "ffmpeg", "-hide_banner", "-loglevel", "error",
    #         "-ss", start, "-to", end, "-i", str(src_path),
    #         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    #         "-c:a", "aac", "-b:a", "192k",
    #         str(out_path)
    #     ])

    # # Not Fast but Accurate & Reencoding
    # else:
    #     def to_sec(t):
    #         h,m,s = t.split(":")
    #         return int(h)*3600 + int(m)*60 + float(s)
    # 
    #     dur = to_sec(end) - to_sec(start)
    # 
    #     run([
    #         "ffmpeg", "-hide_banner", "-loglevel", "error",
    #         "-ss", start, "-i", str(src_path),
    #         "-t", str(dur),
    #         "-c", "copy",
    #         "-avoid_negative_ts", "1",
    #         str(out_path)
    #     ])
    
    # # Fast + ACCURATE + REENCODE
    # run([
    #     "ffmpeg", "-hide_banner", "-loglevel", "error",
    #     "-ss", start, "-to", end, "-i", str(src_path),
    #     "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    #     "-c:a", "aac", "-b:a", "192k",
    #     str(out_path)
    # ])
    
    # Excellent version: FAST + ACCURATE + REENCODE  + BONUS (Better output)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", start,
    ]
    if end and end != "00:00:00":
        cmd += ["-to", end]
    cmd += [
        "-i", str(src_path),
        "-vf", "scale=1280:-1",
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path)
    ]
    r = run(cmd, capture_output=True, text=True)
    ok = bool(r and r.returncode == 0)
    if not ok:
        err = (r.stderr or "").strip() if r else "ffmpeg introuvable"
        print(f"[ffmpeg error] returncode={r.returncode if r else 'N/A'}")
        if err:
            print(f"[ffmpeg] {err[-800:]}")
    return ok

# ======================================================================
# ----------------------------- LINKER
# “Try to create a hardlink (super fast). If that fails, do a full file copy. If destination already exists, do nothing.”
# ======================================================================
def is_admin():
    """Check if script runs with admin rights (Windows)."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def relaunch_as_admin():
    """Relaunch the current script with admin privileges."""
    params = " ".join([f'"{a}"' for a in sys.argv])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit(0)

def same_volume(a: Path, b: Path):
    """Check if two paths are on the same drive/volume."""
    return a.drive.lower() == b.drive.lower()

def try_symlink(src: Path, dst: Path) -> bool:
    """
    Crée un symlink dst → src (aucune duplication de données).
    Fallback hardlink si les symlinks ne sont pas autorisés (pas de Mode Dev Windows).
    NE fait PAS de copie — on ne veut jamais 2 exemplaires du fichier source.
    """
    if not src.exists():
        raise RuntimeError(f"Source introuvable : {src}")

    # Nettoie un symlink cassé existant
    if dst.is_symlink():
        print(f"⚠️ Symlink cassé existant → suppression : {dst.name}")
        dst.unlink()

    if dst.exists():
        if dst.is_symlink():
            # Symlink valide → bon
            print(f"ℹ️  Symlink SRC déjà présent → skip : {dst.name}")
            return True
        else:
            # Fichier normal (ancienne copie) → on supprime et on recrée en symlink
            print(f"🔄 Ancienne copie détectée → remplacement par symlink : {dst.name}")
            dst.unlink()

    dst.parent.mkdir(parents=True, exist_ok=True)

    # 1) -------- SYMLINK (préféré — pointe vers FULL.mp4, zéro espace) --------
    try:
        os.symlink(src.resolve(), dst)
        print(f"🔗 Symlink créé : {dst.name} → {src.name}")
        return True
    except (OSError, NotImplementedError) as e:
        print(f"⚠️ Symlink échoué ({e}) → fallback hardlink")

    # 2) -------- HARDLINK (fallback — même volume requis, zéro espace aussi) --------
    if same_volume(src, dst):
        try:
            os.link(src, dst)
            print(f"🔗 Hardlink créé : {dst.name}")
            return True
        except Exception as e:
            print(f"⚠️ Hardlink échoué ({e})")

    raise RuntimeError(
        f"Impossible de créer un lien pour {dst.name}.\n"
        f"Sur Windows, activez le Mode Développeur pour autoriser les symlinks sans droits admin.\n"
        f"Paramètres → Confidentialité & sécurité → Pour les développeurs → Mode développeur : ON"
    )


def try_link_or_copy(src: Path, dst: Path) -> bool:
    """Try hardlink → copy2. Returns True on success, raises RuntimeError on total failure."""

    if not src.exists():
        raise RuntimeError(f"Source introuvable : {src}")

    # dst.exists() returns False for dangling symlinks — check is_symlink() too
    if dst.is_symlink():
        print(f"⚠️ Symlink cassé détecté → suppression : {dst.name}")
        dst.unlink()

    if dst.exists():
        print(f"⚠️ Destination déjà existante → skip")
        return True

    dst.parent.mkdir(parents=True, exist_ok=True)

    # 1) -------- HARDLINK (instantané, zéro espace, même volume requis) --------
    if same_volume(src, dst):
        try:
            os.link(src, dst)
            print(f"✅ Hardlink créé : {dst.name}")
            return True
        except Exception as e:
            print(f"⚠️ Hardlink échoué : {e}")

    # 2) -------- COPY (fallback universel) --------
    try:
        shutil.copy2(src, dst)
        print(f"✅ Copie effectuée : {dst.name}")
        return True
    except Exception as e:
        raise RuntimeError(f"Impossible de créer {dst.name} : {e}") from e
