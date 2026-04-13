# core/item_processor.py
from pathlib import Path
from core.media import download_to_cache, try_symlink, cut
from core.sources import SourceType, get_video_source_type, get_item_link, resolve_original_video_merged
from core.utils import clean_path, safe_filename


# ============================================
# afficher/debug show a video item data
def print_item(i, it=None):
    # --- si argument sous forme de tuple (idx, dict)
    if it is None and isinstance(i, tuple) and len(i) == 2:
        i, it = i  # déballage propre

    # --- lien et type
    link = it.get("source", "").strip().strip('"').strip("'")
    source_type = get_video_source_type(link)
    
    # --- variables propres
    title: str = it.get("title", "-") # it.get("title") SANS fallback. Si "title" existe pas → retourne None /// it.get("title", "-") # AVEC fallback. Si "title" existe pas → retourne "-"
    start_time: str = it.get("start", "-")
    end_time: str = it.get("end", "-")
    source_display: str = it.get("source", "-")

    # ==========  ========== ==========
    # --- uniLigne
    # print(f"{i:02d} | {title}  {start_time} → {end_time}  [{source_type}]  → {source_display}")

    # --- multilignes
    print(
        f"{i:02d} | {title}\n"
        # f"   🎬 Titre  : {title}"
        f"   ⏱️ Début : {start_time} - Fin : {end_time}\n"
        f"   🎬 Type  : {source_type.name}\n"
        f"   🔗 Source: {source_display}\n"
        # f"🔗 Source  : {source_display if source_display else '❌ (utilisera unique link)'}"
    )


 

def process_item(it, ffmpeg_ok=None, cache_dir=None, source_cache=None, out_dir=None, inputs_dir=None, src_dir=None, video_title=None, idx=0, cancel_check=None, on_step=None):
    """
    Traite un seul extrait : détecte source, télécharge la FULL vidéo si YouTube,
    puis découpe VIDEO et crée SRC à partir du fichier local complet.

    Structure de sortie :
        out_dir/    → fichiers VIDEO (résultat de la découpe)
        inputs_dir/ → FULL.mp4 téléchargé (YouTube), partagé entre extraits
        src_dir/    → SRC link (dans le dossier du titre video, à côté des sous-dossiers extraits)

    video_title : titre de la vidéo source (utilisé pour nommer FULL.mp4 + SRC partagés)
    """

    # inputs_dir fallback: si non fourni, utilise out_dir
    _inputs = Path(inputs_dir) if inputs_dir else Path(out_dir)
    # src_dir: dossier où mettre le SRC link (video title folder). Fallback = inputs_dir
    _src = Path(src_dir) if src_dir else _inputs
    # Titre de la vidéo source (pour le FULL + SRC partagés entre extraits du même video)
    _video_title = video_title or it.get("title", "video")

    def _step(msg: str):
        print(f"→ {msg}")
        if on_step:
            on_step(msg)

    # --------------------
    def processForVideo_LOCAL(source, start, end):
        src_path = Path(source)
        _step(f"Vérification source...")
        print(f"   {src_path}")

        if not src_path.exists():
            raise RuntimeError(f"Fichier source introuvable : {src_path}")

        _step(f"Découpe FFmpeg  {start} → {end or 'fin'}")
        ok = cut(ffmpeg_ok, src_path, start, end, out_video)
        print(f"{'✅' if ok else '❌'} VIDEO → {out_video.name}")
        if not ok:
            raise RuntimeError(
                f"FFmpeg a échoué pour {out_video.name}.\n"
                f"Timestamps : {start} → {end}\n"
                f"Vérifie que ffmpeg est installé et que les timestamps sont valides."
            )

        _step("Création lien source...")
        try_symlink(src_path, out_src)
        print(f"🔗 SRC (symlink) → {out_src.name}")

    def processForVideo_YOUTUBE_CLIP(source, start, end):
        raise RuntimeError(f"YouTube Clips non supportés. Utilisez l'URL de la vidéo originale.")

    def processForVideo_YOUTUBE(it, ffmpeg_ok, out_dir: Path, idx: int):
        source = it.get("source")
        title  = it.get("title", f"Extrait {idx:02d}")
        print(f"▶️ Source YouTube : {source}")

        # 0) Résolution URL
        _step("Résolution URL YouTube...")
        try:
            resolved_url, start_resolved, end_resolved, vid = resolve_original_video_merged(
                source, it.get("start"), it.get("end")
            )
        except Exception as e:
            raise RuntimeError(f"Résolution URL échouée : {e}") from e

        start = start_resolved or "00:00:00"
        end   = end_resolved   or ""
        print(f"   URL résolue : {resolved_url}")
        print(f"   Timestamps  : {start} → {end or 'fin'}")

        # 1) Télécharger si pas en cache — va dans inputs_dir (pas out_dir)
        #    Utilise _video_title (pas le titre de l'extrait) pour partager le FULL entre extraits du même video
        safe_title = safe_filename(_video_title)
        full_video_path = _inputs / f"{safe_title} - FULL.mp4"
        if not full_video_path.exists():
            _step(f"Téléchargement en cours...")
            result = download_to_cache(resolved_url, _inputs, {}, target_file=full_video_path)
            if result is None or not full_video_path.exists():
                raise RuntimeError(
                    f"Téléchargement échoué.\n"
                    f"URL : {resolved_url}\n"
                    f"→ Vérifie : yt-dlp à jour ? (pip install -U yt-dlp)\n"
                    f"→ Vérifie : URL YouTube accessible ?"
                )
            print(f"✅ Vidéo téléchargée : {full_video_path.name}")
        else:
            _step("Vidéo déjà en cache...")
            print(f"ℹ️  Cache : {full_video_path.name}")

        # 2) Découper
        processForVideo_LOCAL(full_video_path, start, end)

    # --------------------
    base = f"{idx:02d} - {safe_filename(it.get('title','Extrait'))}"
    out_video = Path(out_dir) / f"{base} - VIDEO.mp4"                      # résultat → out_dir (extract subfolder)
    out_src   = _src / f"{safe_filename(_video_title)} - SRC.mp4"          # source   → src_dir (video title folder)

    # --- Récupération des informations
    start, end = it.get("start"), it.get("end")
    source = it.get("video_url") or it.get("source")
    src_type = get_video_source_type(source)

    # ==================== TRAITEMENT SELON LE TYPE ====================
    if src_type is SourceType.LOCAL:
        processForVideo_LOCAL(source, start, end)

    elif src_type is SourceType.YOUTUBE:
        processForVideo_YOUTUBE(it, ffmpeg_ok, out_dir, idx)

    elif src_type is SourceType.YOUTUBE_CLIP:
        processForVideo_YOUTUBE_CLIP(source, start, end)

    else:
        raise RuntimeError(
            f"Source inconnue ou introuvable : {source}\n"
            f"Types supportés : fichier local, URL YouTube"
        )
