#!/bin/bash

# ===================================================
#  DEMARRAGE DU BACKEND CUTLY (LINUX / RASPBERRY)
# ===================================================

# Port lu depuis .env (fallback 8000)
USER_PORT=$(grep -E "^PORT=" .env 2>/dev/null | cut -d= -f2 | tr -d ' ')
USER_PORT=${USER_PORT:-8000}

echo ""
echo "==================================================="
echo "    DEMARRAGE DU BACKEND CUTLY (Port $USER_PORT)"
echo "==================================================="
echo ""

# ================ KILL PORT
echo "[*] Etape 1 : Liberation du port $USER_PORT..."
fuser -k $USER_PORT/tcp > /dev/null 2>&1
sleep 1
echo "[OK] Voie libre !"
echo ""

# ================ FFMPEG
echo "[*] Etape 2 : Verification de ffmpeg..."
if ! command -v ffmpeg &> /dev/null; then
    echo "[!] ffmpeg non trouve. Installation..."
    sudo apt-get install -y ffmpeg
    echo "[OK] ffmpeg installe."
else
    echo "[OK] ffmpeg detecte."
fi
echo ""

# ================ VIRTUAL ENVIRONMENT
echo "[*] Etape 3 : Verification de l'environnement virtuel (venv)..."
if [ ! -d "venv" ]; then
    echo "[!] Venv absent. Creation en cours..."
    python3 -m venv venv
    echo "[*] Installation des requirements..."
    ./venv/bin/python3 -m pip install --upgrade pip -q
    ./venv/bin/python3 -m pip install -r requirements.txt
    echo "[OK] Venv cree et configure."
else
    echo "[OK] Environnement virtuel detecte."
fi
echo ""

# ================ LANCEMENT (port et config depuis .env)
echo "[*] Lancement du serveur Python..."
echo "==================================================="

./venv/bin/python3 api_server.py

echo ""
echo "[!] Le serveur s'est arrete."
read -p "Appuyez sur Entree pour quitter..."
