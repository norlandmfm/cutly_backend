# core/paths_and_config.py
import os
import json
from pathlib import Path
from datetime import datetime

from core.utils import safe_filename
from core.sources import ytinfo_json


# ======================================================================
# ---------------- PATHS : gestion des directories
# "Path.home() /"  => # Windows: C:\Users\Nonosky  ou # Linux: /home/nonosky  => stocker des configs ou des fichiers utilisateur qui doivent persister.
# "Path(__file__).parent /" => le dossier où se trouve le script Python actuellement situé. fichiers "locaux" au projet (INPUTS, OUTPUTS, bin, etc.)
# "Path.cwd()." => le dossier courant depuis lequel tu lances Python. Change selon d’où tu fais python final.py. Exmple si : dans cmd tu fais: ici: D:\Projets> python Scripts\final.py => Même si ton script est dans D:\Projets\Scripts, CWD reste D:\Projets. En clair, fichiers relatifs à l’endroit actuel, pas forcément au projet
# ======================================================================
def get_pathOf_user_home() -> Path: return Path.home() # 1️⃣ Dossier perso de l’utilisateur (configs persistantes, etc.)
def get_pathOf_project_root() -> Path: return Path(__file__).parent # 2️⃣ Dossier du script (INPUTS, OUTPUTS, bin du projet)
def get_pathOf_execution_cwd() -> Path: return Path.cwd() # 3️⃣ Dossier courant d’exécution (d’où tu lances le script)


ROOT = Path(__file__).parent.parent.resolve()  # backend/


# ---- ffmpeg dans ./bin si présent ----
local_bin = get_pathOf_project_root() / "bin"
os.environ["PATH"] = str(local_bin) + os.pathsep + os.environ["PATH"]

# inputsDirectory = get_pathOf_execution_cwd()
inputsDirectory = get_pathOf_project_root() / "00 INPUTS/"
# inputsDirectory.mkdir(parents=True, exist_ok=True)

outputsDirectory = get_pathOf_project_root() / "00 OUTPUTS/"
# outputsDirectory.mkdir(parents=True, exist_ok=True)

# defaultCacheDirectory = get_pathOf_project_root() / "00 OUTPUTS" / ".cache"
# defaultCacheDirectory.mkdir(parents=True, exist_ok=True) # Ensure it exists (including parents)


def default_outdir(first_item):
    outputsDirectory.mkdir(parents=True, exist_ok=True)

    # -------------- resolve source
    src = first_item.get("source") or first_item.get("video_url") or first_item.get("source")
    src = src.strip().strip('"').strip("'")
    
    # src = Path(clean_path(first_item.get("source")))

    # link = get_item_link(first_item)
    # source_type = get_video_source_type(link)
    
    print("DEBUG first_item:", first_item)
    print("DEBUG src:", src)
    # -------------- resolve source

    # CAS 1 : fichier local
    if src and Path(src).exists():
        name = safe_filename(Path(src).stem)

    # CAS 2 : YouTube
    elif src and ("youtube.com" in src or "youtu.be" in src):
        info = ytinfo_json(src)
        print("DEBUG ytinfo_json:", json.dumps(info, ensure_ascii=False, indent=2) if info else "None")

        if info:
            title = info.get("title") or "YouTube"
            title = safe_filename(title)[:40]
            name = title
        else:
            name = "youTubeVideo"

    else:
        name = "Output"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # out = outputsDirectory / name / stamp
    out = outputsDirectory / f"{name}_{stamp}"

    print(f"DEBUG stamp={stamp} | out={out}")
    return out


# core/paths_and_config.py
def resolve_output_directory(
    base_name: str | None = None,
    custom_dir: Path | str | None = None,
    create_cache: bool = True,
    timestamp: str | None = None
) -> tuple[Path, Path]:
    """
    Résout le dossier de sortie pour un traitement vidéo.
    - base_name : nom de base pour le dossier (ex: nom du fichier TXT ou titre)
    - custom_dir : chemin fourni par l’utilisateur (sera utilisé à la place de outputsDirectory)
    - create_cache : crée un sous-dossier .cache si True
    - timestamp : chaîne à ajouter pour unicité (ex: "20251223_170000"). Sinon généré automatiquement.

    Retourne (out_dir, cache_dir)
    """
    if custom_dir:
        out_dir = Path(custom_dir).expanduser().resolve()
    else:
        out_dir = outputsDirectory

    # ajout du nom de base si fourni
    if base_name:
        safe_name = safe_filename(base_name)
        out_dir = out_dir / safe_name

    # # --------- timestamp automatique si non fourni
    # if timestamp is None:
    #     timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # out_dir = out_dir / timestamp

    # création du dossier principal
    out_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------
    # création du cache
    # cache_dir = out_dir / ".cache"
    # if create_cache:
    #     cache_dir.mkdir(exist_ok=True)
    
    # --- FIX: cache global toujours au même endroit
    cache_dir = Path(ROOT) / "__pycache__" / "video_cache"
    if create_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)

    return out_dir, cache_dir


# ============================================================ 
# ---------------- CONFIGS
# ============================================================
CONFIG_PATH = get_pathOf_project_root() / ".config.json"
# CONFIG_PATH = get_pathOf_user_home() / ".config.json"


def load_cfg():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    # --- version longue avec try/except
    # if CONFIG_PATH.exists():
    #     try:
    #         return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    #     except Exception:
    #         pass
    # return {}

def save_cfg(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # --- version longue avec try/except
    # try:
    #     CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # except Exception:
    #     pass
