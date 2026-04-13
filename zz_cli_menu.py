# zz_cli_menu.py
# ========================== VERSION UTILISEE POUR FONCTIONNER EN CONSOLE SUR DESKTOP
import sys
from pathlib import Path

# ---- PYTHON PATH (AVANT TOUS LES IMPORTS PROJET)
ROOT = Path(__file__).parent.resolve()  # dossier backend
sys.path.insert(0, str(ROOT))

# ---- imports standards
import shutil
import subprocess
from datetime import datetime
from zz_tool_backend import process_txt_file
from core.item_processor import print_item, process_item
from core.media import download_to_cache, try_link_or_copy, cut
from core.utils import ensure_requirements, parse_items, parse_text, clean_path, safe_filename
from core.paths_and_config import  default_outdir, inputsDirectory, outputsDirectory, load_cfg, resolve_output_directory, save_cfg
from core.sources import get_video_source_type, get_item_link, SourceType, ytinfo_json, resolve_original_video_merged


# ------------------------- FONCTIONS UTILES RÉUTILISABLES LE CLI MENU-------------------------
def choose_txt_file(cfg) -> Path | None:
    """Choix du fichier .txt à traiter"""
    txts = list(inputsDirectory.glob("*.txt"))
    current_txt = None

    if not txts:
        path = input("Aucun .txt trouvé. Chemin du .txt : ").strip().strip('"').strip("'")
        if not path:
            print("❌ Aucun fichier fourni. Abort.")
            return None
        current_txt = Path(path)
    elif len(txts) == 1:
        print(f"   → 1 seul .txt trouvé : {txts[0].name}")
        path = input("Appuie sur ENTER pour utiliser ce fichier ou entre un autre chemin : ").strip().strip('"').strip("'")
        current_txt = Path(path) if path else txts[0]
    else:
        print("\n Plusieurs .txt détectés dans le INPUT FOLDER: ")
        for i, t in enumerate(txts, 1):
            print(f"   {i}) {t.name}")

        choice2 = input(f"\nChoisis un fichier par numéro (1-{len(txts)}) ou entre un chemin : ").strip().strip('"').strip("'")
        if choice2.isdigit() and 1 <= int(choice2) <= len(txts):
            current_txt = txts[int(choice2)-1]
        else:
            current_txt = Path(choice2)

    if not current_txt.exists():
        print("❌ Fichier introuvable. Abort.")
        return None

    cfg["last_txt"] = str(current_txt)
    save_cfg(cfg)
    return current_txt

# def choose_output_dir(cfg, current_out_dir: Path) -> tuple[Path, Path]:
#     """Choix du dossier de sortie et création cache_dir"""
#     print(f"\nDossier de sortie actuel : {current_out_dir}")
#     out_dir_input = input("Nouveau dossier de sortie (ENTER pour garder le même) : \n").strip()
#     if out_dir_input:
#         current_out_dir = Path(out_dir_input).expanduser().resolve()
#     current_out_dir.mkdir(parents=True, exist_ok=True)
#     cache_dir = current_out_dir / ".cache" # locally in the outputed folder of the video item
#     cache_dir.mkdir(exist_ok=True)
# 
#     cfg["last_outdir"] = str(current_out_dir)
#     save_cfg(cfg)
#     return current_out_dir, cache_dir


def choose_output_dir(cfg, current_out_dir: Path | None = None) -> tuple[Path, Path]:
    """
    Choix du dossier de sortie et création automatique du cache.
    Utilise resolve_output_directory pour uniformiser les chemins.
    """
    from core.paths_and_config import resolve_output_directory, save_cfg

    print(f"\nDossier de sortie actuel : {current_out_dir or outputsDirectory}")
    out_dir_input = input("Nouveau dossier de sortie (ENTER pour garder le même) : \n").strip()
    custom_dir = out_dir_input if out_dir_input else current_out_dir

    # on peut ajouter le nom de base à partir de la config si besoin, sinon None
    base_name = None

    # résolution via la fonction centrale
    out_dir, cache_dir = resolve_output_directory(
        base_name=base_name,
        custom_dir=custom_dir
    )

    cfg["last_outdir"] = str(out_dir)
    save_cfg(cfg)

    return out_dir, cache_dir


def select_itemsLOW(items: list[dict]) -> list[dict]:
    """Sélection des extraits à traiter"""
    for i, it in enumerate(items, 1):
        print_item(i, it)

    print("\n📌 Sélectionnez les extraits à traiter (numéros séparés par , ou 'all/tous/tout/toutes ..' pour tous) :")
    selection = input("Votre choix [ENTER = tout] : ").strip().lower()

    if not selection or selection in ("*", "all", "tous", "tout", "toutes",
                                    "All", "Tous", "Tout", "Toutes",
                                    "ALL", "TOUS", "TOUT", "TOUTES"):
        items_to_process = items
    else:
        sel_indices = []
        for x in selection.split(","):
            x = x.strip()
            if x.isdigit() and 1 <= int(x) <= len(items):
                sel_indices.append(int(x)-1)
        items_to_process = [items[i] for i in sel_indices]

    # if not items_to_process:
    #     print("❌ Aucun extrait valide sélectionné. Abort.")
    #     continue

    # Vérification source valide
    has_valid_source = any(it.get("source") for it in items_to_process)
    if not has_valid_source:
        print("❌ Aucun extrait avec source valide. Abort.")
        return []

    # Confirmation avant traitement
    print("\n📌 Confirmation des extraits :")
    for i, it in enumerate(items, 1):
        print_item(i, it)
    input("\n✅ Appuie sur ENTER pour lancer les téléchargements et découpes (CTRL+C pour annuler)")

    return items_to_process
    
# -- avancés
def select_items(items: list[dict], indexed_items: list[tuple[int, dict]] | None = None) -> list[dict]:
    """
    Sélection des extraits à traiter.
    - items : liste d'objets dict
    - indexed_items : si fournie, liste de tuples (index_affiché, dict) pour gérer local_items
    """
    if indexed_items:
        # Utiliser indexed_items pour l'affichage
        for i, it in indexed_items:
            print_item(i, it)
        items_for_selection = indexed_items
        use_indexed = True
    else:
        # Affichage classique
        for i, it in enumerate(items, 1):
            print_item(i, it)
        items_for_selection = list(enumerate(items, 1))
        use_indexed = False

    print("\n📌 Sélectionnez les extraits à traiter (numéros séparés par , ou 'all/tous/tout/toutes ..' pour tous) :")
    selection = input("Votre choix [ENTER = tout] : ").strip().lower()

    if not selection or selection in ("*", "all", "tous", "tout", "toutes",
                                    "All", "Tous", "Tout", "Toutes",
                                    "ALL", "TOUS", "TOUT", "TOUTES"):
        items_to_process = [it for _, it in items_for_selection]
        
    else:
        sel_indices = []
        for x in selection.split(","):
            x = x.strip()
            if x.isdigit():
                idx = int(x)
                # vérifier que l'index entré existe dans la liste affichée
                if any(idx == i for i, _ in items_for_selection):
                    sel_indices.append(idx)

        if use_indexed:
            # indexed_items : les indices sont déjà ceux affichés → pas de -1
            items_to_process = [it for i, it in items_for_selection if i in sel_indices]
        else:
            # liste simple : indices 1-based → on convertit en 0-based
            items_to_process = [items[i-1] for i in sel_indices]

        # Vérification source valide
    has_valid_source = any(it.get("source") for it in items_to_process)
    if not has_valid_source:
        print("❌ Aucun extrait avec source valide. Abort.")
        return []

    # Confirmation avant traitement
    print("\n📌 Confirmation des extraits :")
    for i, it in enumerate(items, 1):
        print_item(i, it)
    input("\n✅ Appuie sur ENTER pour lancer les téléchargements et découpes (CTRL+C pour annuler)")

    return items_to_process

def clean_output_folder(i, it, out_dir: Path):
    """Une fois le traitement terminé. On supprimer les multples fichier - SRC téléchargé"""
    base = f"{i:02d} - {it['title'].replace(':','-').replace('/','_')}"
    out_video = out_dir / f"{base} - VIDEO.mp4"
    out_src = out_dir / f"{base} - SRC.mp4"

        # supprimer le SRC juste après
    if out_src.exists():
        out_src.unlink()
            
def process_items(items_to_process: list[dict], ffmpeg_ok, cache_dir: Path, source_cache: dict, out_dir: Path):
    """Traitement des extraits"""
    # ------------- Traitement des items -------------
        # for idx, it in enumerate(items, 1):
        #     print(f"\n▶️ Traitement de l’extrait {idx}/{len(items_to_process)} : {it['title']}")
            # process_item(
            #     it=it,
            #     ffmpeg_ok=ffmpeg_ok,
            #     cache_dir=cache_dir,
            #     source_cache=source_cache,
            #     out_dir=out_dir,
            #     idx=1,
            # )
        #     print(f"✅ Terminé : {it['title']}\n")

    # ------------- Traitement des items -------------
    last_source = None
    for idx, it in enumerate(items_to_process, 1):
        print(f"\n▶️ Traitement de l’extrait {idx}/{len(items_to_process)} : {it['title']}")

        # --- Gestion des sources vides ---
        if not it.get("source") and last_source: it["source"] = last_source
        elif it.get("source"): last_source = it["source"]

        # --- Résolution YouTube clips / lien original ---
        # video_url, start, end = resolve_original_video_merged(it["source"], it["start"], it["end"])
        video_url, start, end, _ = resolve_original_video_merged(it["source"], it["start"], it["end"])
        it["source"] = video_url
        it["start"] = start
        it["end"] = end

        # process item
        process_item(
            it=it,
            ffmpeg_ok=ffmpeg_ok,
            cache_dir=cache_dir,
            source_cache=source_cache,
            out_dir=out_dir,
            idx=1,
        )

        # delete - SRC videos
        clean_output_folder(idx, it, out_dir)
            
        print(f"✅ Terminé : {it['title']}\n")
    
    print(f"\n🎉 Terminé → {out_dir}")



# ==================================== MON CLI MENU
def launch_command_line_menu(cfg=None, ffmpeg_ok=None):
    """
    Initialise les variables et prépare le menu principal.
    La boucle while True sera remplie avec les options existantes.
    """

    if cfg is None:
        cfg = load_cfg()
    if ffmpeg_ok is None:
        ffmpeg_ok = ensure_requirements()

    # chemins mémorisés
    last_txt = cfg.get("last_txt")
    last_out = cfg.get("last_outdir")
    current_txt = Path(last_txt) if last_txt else None

    # # Création automatique du dossier de sortie basé sur le nom du .txt
    # txt_name = current_txt.stem if current_txt else "default"
    # stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # # out_dir = outputsDirectory / txt_name
    # out_dir: Path = Path(outputsDirectory) / f"{txt_name}_{stamp}"
    # # out_dir = Path(last_out) if last_out else outputsDirectory
    # out_dir.mkdir(parents=True, exist_ok=True) # ensure exist

    # cache_dir = out_dir / ".cache" # backup defaultCacheDirectory
    # cache_dir.mkdir(parents=True, exist_ok=True) # ensure exist
    # source_cache = {}

    # --- Résolution du dossier de sortie et du cache
    out_dir, cache_dir = resolve_output_directory(custom_dir=Path(last_out) if last_out else None)
    source_cache = {}


    # --------------------------- Info initiale ---------------------------
    print(f"\n📂 Inputs folder  : {inputsDirectory}")
    print(f"📂 Outputs folder : {out_dir}")

    # Liste des items parsés
    items = []
    if current_txt and current_txt.exists():
        try:
            items = parse_text(current_txt.read_text(encoding="utf-8", errors="ignore"))
            print(f"📝 Fichier chargé: {current_txt}")
            print(f"✅ {len(items)} extraits détectés (mémoire).")
        except Exception:
            print("⚠️ Impossible de lire le fichier précédent.")


   # --------------------------- MENU ---------------------------
    while True:
        print("\n===== MENU =====")
        print("1) Aperçu des extraits parsés")
        print("2) Proceed / Exécuter extraction")
        print("3) Découper video / 'Cut only' (for local videos only)")
        print("5) Ouvrir le dossier de sortie")
        print("6) Nettoyage / Vider cache")
        print("0) Quitter")
        choice = input("> ").strip()

        # --------------------------- Aperçu ---------------------------
        if choice == "1":
            if not items:
                if current_txt and current_txt.exists():
                    text = current_txt.read_text(encoding="utf-8", errors="ignore")
                    items = parse_text(text)
                if not items:
                    print("ℹ️ Aucun extrait à afficher. Lance l'option 2 pour choisir un fichier.")
                    continue
            print("\n🔍 Aperçu des extraits détectés :\n")
            for i, it in enumerate(items, 1):
                print_item(i, it)

        # --------------------------- Full Extraction ---------------------------
        elif choice == "2":
            # ------------- Choix du fichier .txt -------------
            current_txt = choose_txt_file(cfg)
            if not current_txt:
                continue

            # ------------- Choix du dossier de sortie -------------
            # out_dir, cache_dir = choose_output_dir(cfg, out_dir)
            out_dir, cache_dir = choose_output_dir(cfg, current_out_dir=out_dir)


            # ------- Lecture et parse -------------
            items = parse_items(current_txt)
            if not items:
                continue

            # # ------------- Sélection des extraits à traiter -------------
            # items_to_process = select_items(items)
            # if not items_to_process:
            #     continue

            # # ------------- Traitement des items -------------
            # process_items(items_to_process, ffmpeg_ok, cache_dir, source_cache, out_dir)

            
            items_to_process = select_items(items)
            process_txt_file(
                txt_path=current_txt,
                ffmpeg_ok=ffmpeg_ok,
                custom_dir=out_dir,
            )


        # ---------- CUT ONLY - for local videos nly
        elif choice == "3":
            # 1️⃣ Choix du fichier .txt
                current_txt = choose_txt_file(cfg)
                if not current_txt:
                    continue

                # 2️⃣ Lecture et parse des items
                items = parse_items(current_txt)
                if not items:
                    continue

                # 3️⃣ Filtrage des items locaux (CUT ONLY nécessite des vidéos locales)
                local_items = [(i, it) for i, it in enumerate(items, 1) if get_video_source_type(get_item_link(it)) == SourceType.LOCAL]
                
                if not local_items:
                    print("❌ Aucun extrait local trouvé. CUT ONLY nécessite des vidéos locales.")
                    continue
                
                # Affichage des items par type
                print("\n============ Extraits trouvées ============")
                for i, it in enumerate(items, 1): print_item(i, it)

                print(f"\n============ ℹ️ {len(local_items)} extrait(s) local(aux) détecté(s).============")
                # for item in local_items: print_item(item)
                for idx, it in local_items: print_item(idx, it) # le tuple directement
                # for item in local_items: print_item(*item) # le tuple directement

                
                # 4️⃣ Sélection des extraits à traiter
                # items_to_process = select_items(local_items)
                items_to_process = select_items(items, indexed_items=local_items)

                if not items_to_process:
                    print("❌ Aucun extrait local valide sélectionné. Abort.")
                    continue

                # 5️⃣ Choix du dossier de sortie basé sur le premier item local
                out_dir = default_outdir(items_to_process[0])
                out_dir.mkdir(parents=True, exist_ok=True)

                # 6️⃣ Découpe des vidéos locales
                for i, it in enumerate(items_to_process, 1):
                    src = Path(clean_path(it.get("source")))
                    base = f"{i:02d} - {it['title'].replace(':','-').replace('/','_')}"
                    out_video = out_dir / f"{base} - VIDEO.mp4"
                    out_src = out_dir / f"{base} - SRC.mp4"

                    cut(ffmpeg_ok, src, it["start"], it["end"], out_video)
                    try_link_or_copy(src, out_src)

                    # delete - SRC videos
                    clean_output_folder(i, it, out_dir)

                print(f"\n🎉 Terminé → {out_dir}")

        # --------------------------- Ouvrir dossier sortie ---------------------------
        elif choice == "5":
            try:
                if sys.platform.startswith("win"):
                    os.startfile(out_dir)  # type: ignore
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(out_dir)])
                else:
                    subprocess.run(["xdg-open", str(out_dir)])
            except Exception:
                print(f"📂 {out_dir}")

        # --------------------------- Nettoyage / Cache ---------------------------
        elif choice == "6":
            print("\n--- Nettoyage / Suppression ---")
            print("1) Supprimer tous les fichiers '* - SRC.mp4'")
            print("2) Supprimer tous les fichiers '* - VIDEO.mp4'")
            print("3) Supprimer les deux types")
            print("4) Supprimer le cache vidéo (.cache)")
            print("5) Annuler")
            sub = input("> ").strip()
            targets = []

            if sub == "1":
                targets = list(out_dir.glob("* - SRC.mp4"))
            elif sub == "2":
                targets = list(out_dir.glob("* - VIDEO.mp4"))
            elif sub == "3":
                targets = list(out_dir.glob("* - SRC.mp4")) + list(out_dir.glob("* - VIDEO.mp4"))
            elif sub == "4":
                if cache_dir.exists():
                    try:
                        shutil.rmtree(cache_dir)
                        print(f"🧹 Cache {cache_dir} vidé.")
                    except Exception as e:
                        print(f"⚠️ {e}")
                else:
                    print("ℹ️ Pas de cache à vider.")
                continue
            else:
                print("↩️ Annulé.")
                continue

            if not targets:
                print("ℹ️ Rien à supprimer.")
                continue

            print(f"⚠️ {len(targets)} fichiers vont être supprimés.")
            go = input("Confirmer ? [y/N]: ").strip().lower()
            if go == "y":
                for f in targets:
                    try:
                        f.unlink()
                        print(f"🗑️ {f.name}")
                    except Exception:
                        print(f"⚠️ Impossible: {f.name}")
                print("✅ Nettoyage terminé.")
            else:
                print("↩️  Annulé.")

        # --------------------------- Quitter ---------------------------
        elif choice == "0":
            print("👋 Bye!")
            break

        else:
            print("❔ Choix inconnu.")

        pass