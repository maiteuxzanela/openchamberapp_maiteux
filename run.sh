#!/bin/bash
exec > "$HOME/openchamber-app/debug.log" 2>&1

# Inclui as pastas dos binarios no PATH
export PATH="$HOME/.opencode/bin:$HOME/.local/node/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Define o caminho exato do binario do opencode
export OPENCODE_BINARY="/home/maiteuxzanela/.opencode/bin/opencode"

OPENCHAMBER_BIN="$HOME/.local/node/bin/openchamber"
SERVER_PID=""

cleanup() {
    if [ "$OPENCHAMBER_KEEP_SERVER" = "1" ]; then
        return 0
    fi
    if [ -n "$SERVER_PID" ]; then
        kill -TERM "$SERVER_PID" 2>/dev/null || true
        "$OPENCHAMBER_BIN" stop 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Inicia o OpenChamber caso a porta 3000 nao responda
if ! curl -s http://127.0.0.1:3000 > /dev/null 2>&1; then
    echo "Iniciando OpenChamber..."
    "$OPENCHAMBER_BIN" &
    SERVER_PID=$!
    
    # Aguarda ate 15 segundos para o servidor subir
    for i in {1..15}; do
        if curl -s http://127.0.0.1:3000 > /dev/null 2>&1; then
            echo "Servidor pronto na tentativa $i"
            break
        fi
        sleep 1
    done
else
    export OPENCHAMBER_KEEP_SERVER=1
fi

# Abre a interface grafica
"$HOME/openchamber-app/venv/bin/python" "$HOME/openchamber-app/app.py"
