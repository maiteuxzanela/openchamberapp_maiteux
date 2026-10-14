#!/bin/bash
exec > "$HOME/openchamber-app/debug.log" 2>&1

# Inclui as pastas dos binarios no PATH
export PATH="$HOME/.opencode/bin:$HOME/.local/node/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Define o caminho exato do binario do opencode
export OPENCODE_BINARY="/home/maiteuxzanela/.opencode/bin/opencode"

# >>> OPENCHAMBER_ARG_PARSE_BEGIN
# TICKET-DESKTOP-09: repassa os argumentos CLI e ativa o debug sob demanda.
# ./run.sh --debug (ou -d) exporta OPENCHAMBER_DEBUG=1, que o app.py traduz
# em webview.start(debug=...) via resolve_debug_mode(). Argumentos
# desconhecidos sao ignorados aqui e seguem intactos para o app.py.
parse_openchamber_args() {
    for arg in "$@"; do
        case "$arg" in
            --debug|-d)
                OPENCHAMBER_DEBUG=1
                export OPENCHAMBER_DEBUG
                ;;
        esac
    done
}
parse_openchamber_args "$@"
# <<< OPENCHAMBER_ARG_PARSE_END

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

# Abre a interface grafica (argumentos CLI repassados ao app.py)
"$HOME/openchamber-app/venv/bin/python" "$HOME/openchamber-app/app.py" "$@"
