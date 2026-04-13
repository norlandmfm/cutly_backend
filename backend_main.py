# backend_main.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import os

# ---- BOOTSTRAP PATH
ROOT = os.path.dirname(os.path.abspath(__file__))  # backend/
sys.path.insert(0, ROOT)  # <<-- important: insert en tête

# ---- imports projet
from core.paths_and_config import load_cfg
from core.sources import get_video_source_type
from core.utils import ensure_requirements
from zz_cli_menu import launch_command_line_menu

# ============================================ MAIN MENU
if __name__ == "__main__":
    cfg = load_cfg()
    ffmpeg_ok = ensure_requirements()
    launch_command_line_menu(cfg, ffmpeg_ok)
