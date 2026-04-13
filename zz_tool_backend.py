# backend\zz_tool_backend.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ========================== VERSION UTILISEE POUR FONCTIONNER AVEC MON FLUTTER
import sys
import os
import io
import json
import shutil
import argparse
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ------------------------------------------------------------------
# bootstrap path
ROOT = Path(__file__).parent.resolve()
sys.path.append(str(ROOT))

# ------------------------------------------------------------------
# imports EXISTANTS
from core.media import cut, download_to_cache, try_link_or_copy
from core.utils import ensure_requirements, clean_path, parse_items, safe_filename
from core.item_processor import process_item
from core.sources import SourceType, get_video_source_type, resolve_original_video_merged
from core.paths_and_config import outputsDirectory, resolve_output_directory


# ------------------------------------------------------------------
def log(msg: str):
    """stdout simple et lisible pour Flutter"""
    print(msg, flush=True)

# ------------------------------------------------------------------
def build_item(args) -> dict:
    """Construit un item compatible avec item_processor"""
    return {
        "title": args.title or "untitled",
        "source": args.url,
        "start": str(args.start),  # conversion en string
        "end": str(args.end),      # conversion en string
    }

# # ------------------------------------------------------------------
# def process_single(item, ffmpeg_ok, cache_dir, source_cache, out_dir):
#     """Traite un seul extrait"""
#     try:
#         # Resolve YouTube clips → original video if needed
#         video_url, start, end, _ = resolve_original_video_merged(
#             item["source"],
#             item["start"],
#             item["end"],
#         )
#         item["source"] = video_url
#         item["start"] = start or "00:00:00"
#         item["end"] = end or ""

#         log(f"▶️ Processing: {item['title']}")
#         process_item(
#             it=item,
#             ffmpeg_ok=ffmpeg_ok,
#             cache_dir=cache_dir,
#             source_cache=source_cache,
#             out_dir=out_dir,
#             idx=1
#         )

#         log(f"✅ Done: {item['title']}\n")
#     except Exception as e:
#         log(f"❌ Error processing {item.get('title','?')}: {e}")

def process_single(item, ffmpeg_ok, out_dir):
    try:
        src_path = Path(item.get("_local_source"))

        start = item.get("_start_resolved") or item.get("start") or "00:00:00"
        end   = item.get("_end_resolved")   or item.get("end")   or ""

        log(f"▶️ Processing: {item['title']}")

        base = safe_filename(item.get("title","Extrait"))
        out_video = out_dir / f"{base}.mp4"  # juste le clip

        # découpe
        cut(ffmpeg_ok, src_path, start, end, out_video)

        # ✅ Plus de copie/lien SRC inutile
        # try_link_or_copy(src_path, out_src)

        log(f"✅ Done: {item['title']}\n")

    except Exception as e:
        log(f"❌ Error processing {item.get('title','?')}: {e}")



# ------------------------------------------------------------------
# def process_txt_file(txt_path: Path, ffmpeg_ok, out_dir):
def process_txt_file(txt_path: Path, ffmpeg_ok, custom_dir: Path | None = None):
    """
        Traite un fichier TXT d’extraits vidéo : crée un dossier dédié pour le fichier,
        regroupe les extraits par source (URL ou locale), télécharge les vidéos complètes
        si besoin et découpe chaque extrait avec ffmpeg dans le sous-dossier correspondant.
        Copie également le fichier TXT original dans le dossier de sortie.
    """
    
    items = parse_items(txt_path)
    if not items:
        log("❌ Aucun extrait trouvé dans le TXT. Abort.")
        return

    log(f"📌 {len(items)} extraits détectés dans {txt_path.name}\n")

    # --- Crée le dossier de sortie seulement si items existants
    out_dir, cache_dir = resolve_output_directory(custom_dir=custom_dir)

    # --- Crée un dossier dédié pour ce fichier TXT
    folder_name = safe_filename(txt_path.stem)
    txt_out_dir = out_dir / folder_name
    txt_out_dir.mkdir(parents=True, exist_ok=True)

    # --- Copier le fichier TXT
    shutil.copy2(txt_path, txt_out_dir / txt_path.name)



    # --- Préparer un dict pour les vidéos complètes téléchargées par source
    full_videos = {}  # {resolved_url: Path vers FULL.mp4}

    # --- Préparer un dict pour les sous-dossiers par source
    source_dirs = {}  # {resolved_url_or_path: Path vers sous-dossier}

    for idx, item in enumerate(items, 1):
        source = item.get("source")
        if not source:
            log(f"⚠️ Skip item {idx}: pas de source")
            continue

        src_type = get_video_source_type(source)

        # --- Pour les vidéos YT, résoudre la vidéo originale
        if src_type == SourceType.YOUTUBE:
            resolved_url, start, end, vid = resolve_original_video_merged(
                source, item.get("start"), item.get("end")
            )
            item["_resolved_url"] = resolved_url
            item["_start_resolved"] = start
            item["_end_resolved"] = end
            key = resolved_url  # clé pour ce source unique
        else:
            key = source  # vidéo locale
            item["_resolved_url"] = key

        # --- Crée un sous-dossier par source unique si pas déjà créé
        if key not in source_dirs:
            safe_source_name = safe_filename(str(key))
            source_dir = txt_out_dir / safe_source_name
            source_dir.mkdir(parents=True, exist_ok=True)
            source_dirs[key] = source_dir
        else:
            source_dir = source_dirs[key]

        # --- Télécharger la FULL vidéo si pas déjà fait
        if src_type == SourceType.YOUTUBE and key not in full_videos:
            full_path = source_dir / f"{safe_filename(str(key))} - FULL.mp4"
            if full_path.exists() and full_path.stat().st_size > 3_000_000:
                log(f"ℹ️ FULL vidéo déjà existante : {full_path}")
            else:
                log(f"▶️ Téléchargement FULL vidéo : {resolved_url}")
                download_to_cache(resolved_url, source_dir, {}, target_file=full_path)

            full_videos[key] = full_path

        # --- Fournir la source locale pour cet extrait
        if src_type == SourceType.YOUTUBE:
            item["_local_source"] = full_videos[key]
        else:
            item["_local_source"] = source

        # --- Traiter chaque extrait dans son sous-dossier source
        process_single(item, ffmpeg_ok, source_dir)


# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Cutly backend tool (Flutter ready)")

    # Mode single
    parser.add_argument("--url", type=str, help="Video URL or local path")
    parser.add_argument("--start", type=float, help="Start time (seconds)")
    parser.add_argument("--end", type=float, help="End time (seconds)")
    parser.add_argument("--title", type=str, default="clip", help="Clip title")

    # Mode batch TXT
    parser.add_argument("--txt", type=str, help="Path to TXT file with multiple items")
    parser.add_argument("--out", type=str, default=str(outputsDirectory), help="Output folder")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # requirements
    ffmpeg_ok = ensure_requirements()
    if not ffmpeg_ok:
        log("❌ ffmpeg / requirements not ready")
        sys.exit(1)

    # ------------------------------------------------------------------
    # output dirs
    # out_dir = Path(args.out).expanduser().resolve()
    # out_dir.mkdir(parents=True, exist_ok=True)
    # cache_dir = out_dir / ".cache"
    # cache_dir.mkdir(exist_ok=True)
    # source_cache = {}

    out_dir, cache_dir = resolve_output_directory(custom_dir=Path(args.out))
    source_cache = {}


    # ------------------------------------------------------------------
    # Mode batch TXT
    if args.txt:
        txt_path = Path(args.txt).expanduser().resolve()
        if not txt_path.exists():
            log(f"❌ TXT file not found: {txt_path}")
            sys.exit(1)
        # process_txt_file(txt_path, ffmpeg_ok, out_dir)
        # process_txt_file(txt_path, ffmpeg_ok, cache_dir, source_cache, out_dir)
        process_txt_file(txt_path, ffmpeg_ok, custom_dir=out_dir)


    # ------------------------------------------------------------------
    # Mode single
    elif args.url and args.start is not None and args.end is not None:
        item = build_item(args)

        # créer un dossier pour le clip unique
        base_title = safe_filename(item.get("title", "clip"))
        # single_out_dir = out_dir / base_title
        # single_out_dir.mkdir(parents=True, exist_ok=True)
        single_out_dir, _ = resolve_output_directory(custom_dir=out_dir / base_title)


        # --- Détecter le type de source
        src_type = get_video_source_type(item["source"])

        if src_type == SourceType.YOUTUBE:
            # résoudre la vidéo originale
            resolved_url, start, end, vid = resolve_original_video_merged(
                item["source"], item["start"], item["end"]
            )
            item["_local_source"] = download_to_cache(resolved_url, single_out_dir, {}, target_file=single_out_dir / f"{base_title} - FULL.mp4")
            item["_start_resolved"] = start
            item["_end_resolved"] = end
        else:
            # vidéo locale
            item["_local_source"] = Path(item["source"]).expanduser().resolve()

        process_single(item, ffmpeg_ok, single_out_dir)


    else:
        log("❌ Invalid arguments. Either provide --txt <file> or --url/--start/--end")
        sys.exit(1)

    log(f"🎉 All done → {out_dir}")

# ------------------------------------------------------------------
if __name__ == "__main__":
    main()
