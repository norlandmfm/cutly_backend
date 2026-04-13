# core/utils.py
import subprocess
import sys
import shutil
import re
from pathlib import Path

# ======================================================================
# ---------------- SYSTEM Utils
# ======================================================================   
def pip_install(pkg):
    print(f"📦 Installation de {pkg} si nécessaire…")
    run([sys.executable, "-m", "pip", "install", "--upgrade", pkg])

def run(cmd, capture=False, check=False):
    try:
        if capture:
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)
        else:
            return subprocess.run(cmd, check=check)
    except FileNotFoundError:
        return None

def ensure_requirements():
    # yt-dlp
    r = run(["yt-dlp", "--version"], capture=True)
    if not r or r.returncode != 0:
        raise RuntimeError(
            "yt-dlp introuvable. Installez-le avec : pip install yt-dlp\n"
            "Puis redémarrez le backend."
        )
    print(f"✅ yt-dlp {r.stdout.strip()}")

    # ffmpeg
    ff = run(["ffmpeg", "-version"], capture=True)
    if not ff or ff.returncode != 0:
        raise RuntimeError(
            "ffmpeg introuvable. Installez-le avec :\n"
            "  Windows : winget install ffmpeg\n"
            "  ou      : scoop install ffmpeg\n"
            "Puis redémarrez le backend."
        )
    return True


# ======================================================================
# ---------------- FILES Utils
# ======================================================================   


# ======================================================================
# ------------- Parsers and text normalisers...
# ======================================================================
def safe_filename(name): return re.sub(r'[\\/*?:"<>|]+', "_", name).strip()

def normalize_time(t):
    """
    Normalise un timestamp dans un format propre HH:MM:SS.xxx
    Gère toutes les formes possibles :
      - "12"                 → 00:00:12
      - "89.5"               → 00:01:29.5
      - "1:23"               → 00:01:23
      - "12:34:56"           → 12:34:56
      - "0:0:3.450"          → 00:00:03.450
      - ":::45"              → 00:00:45
      - "1:2:3"              → 01:02:03
      - trop de segments     → on garde les 3 derniers (comme normalize_time1)
    
    Suppression automatique des zéros inutiles :
      - 12.000 → 12
      - 12.340 → 12.34
    """

    # Nettoyage de base
    t = t.strip().replace(" ", "")

    # CAS 1 : aucun ":" → c'est un nombre de secondes
    if ":" not in t:
        seconds = float(t)
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".rstrip("0").rstrip(".")

    # Split mais on remplace morceaux vides par 0 (ex: "::12")
    parts = [p if p else "0" for p in t.split(":")]

    # Trop de morceaux → on garde les 3 derniers (ex: "1:2:3:4" prend "2:3:4")
    while len(parts) > 3:
        parts = parts[1:]

    # CAS 2 : MM:SS → 2 segments
    if len(parts) == 2:
        h = 0
        m, s = parts
    # CAS 3 : HH:MM:SS → 3 segments
    elif len(parts) == 3:
        h, m, s = parts
    # CAS improbable mais possible : 1 segment avec ":" vide → fallback
    else:
        h, m, s = 0, parts[0], parts[1] if len(parts) > 1 else 0

    # Conversion
    sec = float(s)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}".rstrip("0").rstrip(".")
    # return f"{int(h):02d}:{int(m):02d}:{int(sec):02d}" if sec.is_integer() else f"{int(h):02d}:{int(m):02d}:{sec:06.3f}".rstrip("0").rstrip(".")

def _norm_label(s: str) -> str:
    """Normalise le label avant les deux-points: enlève puces/espaces,
    met en minuscule, retire accents basiques."""
    s = s.replace("\u00A0", " ")  # NBSP
    s = s.strip()
    # enlève puces/traits éventuels au début
    while s and s[0] in "-–—•*·":
        s = s[1:].lstrip()
    s = s.lower()
    # dé-accentuation simple (suffisant ici)
    trans = str.maketrans("àâäéèêëîïôöûüùç", "aaaeeeeiioouuuc")
    s = s.translate(trans)
    return s

def _maybe_flush_block(cur, items):
    """Si bloc complet → push, et on utilise 'source' au lieu de 'link'."""
    
    if cur.get("title") and cur.get("start") and cur.get("end"):
        items.append({
            "title": cur["title"],
            "start": normalize_time(str(cur["start"])),
            "end":   normalize_time(str(cur["end"])),
            "source": cur.get("source", "").strip()
        })

    cur.clear()

def parse_text(text: str):
    lines = text.splitlines()
    items = []
    cur = {}

    for raw in lines:
        line = raw.replace("\u00A0", " ").strip()
        if not line:
            _maybe_flush_block(cur, items)
            continue
        if re.match(r"^[\s\uFEFF]*\d+\s*[\.\)]\s*$", line):
            _maybe_flush_block(cur, items)
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            key = _norm_label(key)
            val = val.strip()
            if key.startswith("titre"):
                cur["title"] = val
            elif key.startswith("debut"):
                cur["start"] = val
            elif key == "fin":
                cur["end"] = val
            elif key.startswith("source"):
                cur["source"] = val

    _maybe_flush_block(cur, items)

    # --- fallback de la dernière source ---
    last_source = None
    for it in items:
        if not it["source"]:
            it["source"] = last_source
        else:
            last_source = it["source"]

    return items

def clean_path(path_str):
    # src = Path(it["source"])
    # src = Path(it["source"].strip().strip('"').strip("'"))
    # final => src = Path(clean_path(it.get("source")))

    # -------------------------
    # # Avant
    # src = Path(it["source"])
    # # Après
    # src = Path(it["source"].strip().strip('"').strip("'"))

    if not path_str:
        return ""
    return path_str.strip().strip('"').strip("'")

# ---
def parse_items(txt_path: Path) -> list[dict]:
    """Lecture et parsing du fichier .txt"""
    text = txt_path.read_text(encoding="utf-8", errors="ignore")
    items = parse_text(text)
    if not items:
        print("❌ Aucun extrait trouvé. Abort.")
    else:
        print(f"\n✅ {len(items)} extraits détectés.")
    return items
