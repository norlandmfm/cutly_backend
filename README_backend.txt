backend/
├── README_backend.txt
├── __pycache__/
│   └── backend.cpython-313.pyc
├── backend - original FULL.py
├── backend.py
├── cli_menu.py
├── item_processor.py
├── media/
│   ├── cache.py
│   ├── cutter.py
│   └── linker.py
├── parsing.py
├── paths_and_config.py
├── sources/
│   ├── local.py
│   ├── source_types.py
│   └── youtube.py
├── structure.txt
├── utils.py
└── windows - getFolderStructure.py




:::::::::::::::::::::::::::::::::::

backend/
│
├── main.py                  # point d’entrée (menu, orchestration)
│
├── config/
│   ├── paths.py             # gestion des chemins (home, project, inputs, outputs)
│   ├── config_store.py      # load_cfg / save_cfg
│
├── cli/
│   ├── menu.py              # menu principal + routing
│   ├── prompts.py           # inputs utilisateur (choix, confirmations)
│
├── parsing/
│   ├── txt_parser.py        # parse_text, normalize_time, label parsing
│   ├── validators.py        # vérifs items (source, start/end, cohérence)
│
├── sources/
│   ├── source_types.py      # enum SourceType + detection
│   ├── local.py             # logique vidéo locale
│   ├── youtube.py           # yt-dlp, resolve_original_video, ytinfo_json
│
├── media/
│   ├── cutter.py            # ffmpeg cut
│   ├── linker.py            # hardlink / symlink / copy + admin
│   ├── cache.py             # download_to_cache
│
├── processing/
│   ├── item_processor.py    # process_item (dispatch par type)
│   ├── batch_processor.py   # process_items (loop + post-clean)
│
├── utils/
│   ├── system.py            # run(), ensure_requirements(), pip_install()
│   ├── files.py             # safe_filename, clean_path
│
└── README_backend.txt       # doc interne (généré à la demande comme tu aimes)
