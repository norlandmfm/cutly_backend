# core/sources.py
from enum import Enum
from pathlib import Path
import re
import json
from core.utils import run
from core.utils import normalize_time


# déterminer le type de source
class SourceType(Enum):
    LOCAL = 1
    YOUTUBE = 2
    YOUTUBE_CLIP = 3
    UNKNOWN = 4



def get_video_source_type(source: str) -> SourceType:
    if not source:
        return SourceType.UNKNOWN

    source = source.strip().strip('"').strip("'")
    # source_type = "video locale" if Path(source).exists() else "clips YouTube" if "youtube.com" in source or "youtu.be" in source else "aucune"
    
    # 1) Source locale
    if Path(source).exists():
        return SourceType.LOCAL

    # 2) Lien YouTube
    if "youtube.com" in source or "youtu.be" in source:
        # les clips (cutted)
        if "youtube.com/clip/" in source:
            return SourceType.YOUTUBE_CLIP
        
        # les video normal
        return SourceType.YOUTUBE

    # 3) Rien trouvé
    return SourceType.UNKNOWN

def get_item_link(it):
    # return it.get("source", "")
    return it.get("source", "").strip().strip('"').strip("'")



# ====================================================================== YOUTUBE
# ---------------- Résolution Clip -> Vidéo originale
# Gives the REAL YouTube video URL AND correct start/end time, especially for YouTube CLIPS.
# It tries to find the real/original YouTube video URL behind: ytb Shorts / ytb “clips” / ytb Remixes / ytb Mix playlists / Livestream clips / Playlist entries / Redirected URLs / YouTube-dlp resolved metadata
# ======================================================================
def ytinfo_json(url):
    r = run([
        "yt-dlp",
        "--extractor-args", "youtube:player_client=android",
        "-J", url
    ], capture=True)

    # r = run([
    #     "yt-dlp",
    #     "-J", url
    # ], capture=True)

    if not r or r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

def resolve_original_video_merged(url, start_txt=None, end_txt=None):
    """
    Resolve:
      - original video URL (normal, playlist, or YouTube clip)
      - video id
      - start/end times (for clips or provided by user)
    """

    # -----------------------------------------------------------
    # 1) Remove timestamp from URL (e.g. &t=30s)
    # -----------------------------------------------------------
    clean_url = re.sub(r"[&?]t=\d+s", "", url)
    info = ytinfo_json(clean_url)

    if not info:
        # # yt-dlp failed → return the input without modification
        # return url, start_txt, end_txt, None

        # yt-dlp failed → fallback rapide pour clip
        r = run([
            "yt-dlp",
            "--skip-download",
            "--print-json",
            "--extractor-args", "youtube:player_client=android",
            clean_url
        ], capture=True)
        try:
            info = json.loads(r.stdout)
        except Exception:
            return url, start_txt, end_txt, None

    # Extract common fields
    orig = info.get("original_url")
    web  = info.get("webpage_url")
    vid  = info.get("id")

    # Helper to detect actual watch URLs
    def is_watch(u):
        return u and (
            "watch?v=" in u
            or "youtube.com/live/" in u
            or "youtu.be/" in u
        )

    # -----------------------------------------------------------
    # 2) If this is a YouTube CLIP → extract true origin + times
    # -----------------------------------------------------------
    if info.get("extractor") == "youtube:clip":
        # real video link of the clip
        real_origin = orig or web or clean_url
        if not real_origin.startswith("http"):
            real_origin = clean_url


        clip_start = info.get("start_time")
        clip_end   = info.get("end_time")

        # Convert timestamps from clip metadata
        if clip_start is not None and clip_end is not None:
            start = normalize_time(str(clip_start))
            end   = normalize_time(str(clip_end))
        else:
            # fallback to provided text times
            start = start_txt
            end   = end_txt

        return real_origin, start, end, vid

    # -----------------------------------------------------------
    # 3) Normal video (NOT a clip)
    # Try original_url → webpage_url → input
    # -----------------------------------------------------------
    if is_watch(orig):
        resolved = orig
    elif is_watch(web):
        resolved = web
    else:
        resolved = clean_url

    # -----------------------------------------------------------
    # 4) Fallback: sometimes info contains a playlist with entries
    # -----------------------------------------------------------
    entries = info.get("entries")
    if entries and isinstance(entries, list) and entries:
        e0 = entries[0]
        o2 = e0.get("original_url") or e0.get("webpage_url")
        if is_watch(o2):
            # playlist entry origin + its ID
            return o2, start_txt, end_txt, e0.get("id")

    # -----------------------------------------------------------
    # 5) Normal case: URL + provided times + main video ID
    # -----------------------------------------------------------
    return resolved, start_txt, end_txt, vid
    # return url, vid, orig, start_txt, end_txt

