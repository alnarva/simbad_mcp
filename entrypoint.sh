#!/bin/bash
# entrypoint.sh - Arranca Obscura (headless browser) y espera.
# Simbad MCP se ejecuta bajo demanda via `docker exec` para evitar
# múltiples instancias compitiendo por el mismo browser.
# set -e: error handling estricto.
set -e

OBSCURA_BIN="/app/bin/obscura"
OBSCURA_PORT="${OBSCURA_PORT:-9222}"
OBSCURA_WORKERS="${OBSCURA_WORKERS:-1}"
OBSCURA_STEALTH="${OBSCURA_STEALTH:-true}"

# ============================================================
#  Simbad ASCII Banner — estilo Obscura con colores ANSI
# ============================================================
print_banner() {
    WHITE='\033[97m'
    CYAN='\033[36m'
    GRAY='\033[90m'
    YELLOW='\033[33m'
    BRIGHT_CYAN='\033[96m'
    R='\033[0m'

    printf '\n'
    printf '%b' "${WHITE}                             ╱│${R}\n"
    printf '%b' "${WHITE}                            ╱ │${R}\n"
    printf '%b' "${WHITE}                           ╱__│${R}\n"
    printf '%b' "${WHITE}                      ____╱___│____${R}\n"
    printf '%b' "${WHITE}                      \\___________/${R}\n"
    printf '%b' "${WHITE}                  ~~~~~~~~~~~~~~~~~~~${R}\n"
    printf '\n'
    printf '%b' "${CYAN}   ███████╗██╗███╗   ███╗██████╗  █████╗ ██████╗${R}\n"
    printf '%b' "${CYAN}   ██╔════╝██║████╗ ████║██╔══██╗██╔══██╗██╔══██╗${R}\n"
    printf '%b' "${CYAN}   ███████╗██║██╔████╔██║██████╔╝███████║██║  ██║${R}\n"
    printf '%b' "${CYAN}   ╚════██║██║██║╚██╔╝██║██╔══██╗██╔══██║██║  ██║${R}\n"
    printf '%b' "${CYAN}   ███████║██║██║ ╚═╝ ██║██████╔╝██║  ██║██████╔╝${R}\n"
    printf '%b' "${CYAN}   ╚══════╝╚═╝╚═╝     ╚═╝╚═════╝ ╚═╝  ╚═╝╚═════╝${R}\n"
    printf '\n'
    printf '%b' "${BRIGHT_CYAN}            MCP Navigator for AI Agents${R}\n"
    printf '\n'
}

print_banner

echo "⛵ Simbad Engine iniciando..."
echo "🦀 Lanzando Obscura (headless browser) en puerto ${OBSCURA_PORT}..."

# Construir argumentos de Obscura
# Crear directorio para perfil persistente (cookies, sesiones, localStorage)
STORAGE_DIR="/app/db"
mkdir -p "${STORAGE_DIR}"

OBSCURA_ARGS="serve --port ${OBSCURA_PORT} --workers ${OBSCURA_WORKERS}"
OBSCURA_ARGS="${OBSCURA_ARGS} --storage-dir ${STORAGE_DIR}"
if [ "${OBSCURA_STEALTH}" = "true" ]; then
    OBSCURA_ARGS="${OBSCURA_ARGS} --stealth"
    echo "  ✓ Stealth mode activado (anti-detección + tracker blocking)"
fi
echo "  ✓ Perfil persistente: ${STORAGE_DIR}"

# Iniciar Obscura en background
${OBSCURA_BIN} ${OBSCURA_ARGS} 2>&1 &
OBSCURA_PID=$!

# Esperar a que Obscura esté listo
echo "  ⏳ Esperando a que Obscura esté listo..."
for i in $(seq 1 30); do
    if curl -s "http://127.0.0.1:${OBSCURA_PORT}/json/version" > /dev/null 2>&1; then
        echo "  ✓ Obscura listo (PID: ${OBSCURA_PID})"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "  ❌ Error: Obscura no respondió después de 30s"
        kill ${OBSCURA_PID} 2>/dev/null
        exit 1
    fi
    sleep 1
done

# Exportar variable para que Simbad sepa a dónde conectarse
export BROWSER_CDP_URL="http://127.0.0.1:${OBSCURA_PORT}"

# Atrapar señal de parada para limpieza
cleanup() {
    echo ""
    echo "⛵ Simbad deteniéndose..."
    kill ${OBSCURA_PID} 2>/dev/null
    wait ${OBSCURA_PID} 2>/dev/null
    echo "  ✓ Obscura detenido"
    echo "  ⚓ Hasta la próxima!"
    exit 0
}
trap cleanup SIGINT SIGTERM

# ============================================================
#  Vector Database Health Check
# ============================================================
check_vector_db() {
    local DB_PATH="/app/db/simbad_chats.db"
    local BROWSER_DB_PATH="/app/db/simbad_browser.db"
    
    if [ -f "$DB_PATH" ]; then
        local RECORDS=$(python3 -c "
import sqlite3, os
path = '${DB_PATH}'
if os.path.exists(path):
    conn = sqlite3.connect(path)
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) FROM chats_context')
        total = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM chats_context WHERE embedding IS NOT NULL')
        emb = c.fetchone()[0]
        print(f'{total}|{emb}')
    except:
        print('0|0')
    conn.close()
" 2>/dev/null)
        local TOTAL="${RECORDS%%|*}"
        local WITH_EMB="${RECORDS##*|}"
        if [ -n "$TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
            echo "  ✓ Memoria vectorial (Pacific): ${TOTAL} registros, ${WITH_EMB} con embeddings"
        else
            echo "  ⚠ Memoria vectorial (Pacific): BD existe pero vacía o sin embeddings"
        fi
    else
        echo "  ⚠ Memoria vectorial (Pacific): BD no encontrada en ${DB_PATH}"
        echo "    → Ejecuta 'python3 sync_memories.py' en el host antes de arrancar"
    fi
    
    if [ -f "$BROWSER_DB_PATH" ]; then
        local BRECORDS=$(python3 -c "
import sqlite3, os
path = '${BROWSER_DB_PATH}'
if os.path.exists(path):
    conn = sqlite3.connect(path)
    c = conn.cursor()
    try:
        c.execute('SELECT COUNT(*) FROM web_context')
        total = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM web_context WHERE embedding IS NOT NULL')
        emb = c.fetchone()[0]
        print(f'{total}|{emb}')
    except:
        print('0|0')
    conn.close()
" 2>/dev/null)
        local BTOTAL="${BRECORDS%%|*}"
        local BWITH_EMB="${BRECORDS##*|}"
        if [ -n "$BTOTAL" ] && [ "$BTOTAL" -gt 0 ]; then
            echo "  ✓ Memoria vectorial (Atlantic): ${BTOTAL} registros, ${BWITH_EMB} con embeddings"
        else
            echo "  ⚠ Memoria vectorial (Atlantic): BD existe pero vacía"
        fi
    else
        echo "  ⚠ Memoria vectorial (Atlantic): BD de navegación no encontrada (se crea al navegar)"
    fi
}

echo ""
echo "📊 Estado de la Base de Datos Vectorial:"
check_vector_db || true
echo ""

echo "🚀 Simbad listo. CDP: ${BROWSER_CDP_URL}"
echo ""

# ============================================================
#  Mantener el contenedor vivo. El MCP server se ejecuta
#  via `docker exec -d` desde el host.
# ============================================================
while true; do
    sleep 3600 &
    wait $!
done
