#!/bin/bash

# Simbad + Obscura - Installer Script
# Makes Simbad easily portable to any Linux system.
# Ahora incluye Obscura como navegador headless incorporado.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
LAUNCHER_PATH="$PROJECT_DIR/simbad"
SYMLINK_PATH="$BIN_DIR/simbad"

echo "⛵ Preparing to install Simbad (with Obscura engine) from: $PROJECT_DIR"

# 1. Verify system dependencies
echo "🔍 Checking dependencies..."

# Docker check
if ! command -v docker &> /dev/null; then
    echo "❌ Error: Docker is not installed. Please install Docker first."
    exit 1
else
    echo "  ✓ Docker found."
fi

# Ollama check (optional warning)
if ! command -v ollama &> /dev/null; then
    echo "⚠️  Warning: Ollama is not installed. Semantic search features require Ollama."
    echo "  You can install it later from: https://ollama.com"
else
    echo "  ✓ Ollama found."
fi

echo "  ✓ Obscura headless browser se descargará durante el build de Docker."

# 2. Build Docker Image
echo "🐳 Building Simbad Docker image (con Obscura)..."
docker build -t simbad "$PROJECT_DIR"

# 3. Setup local directory permissions for database mount
echo "📁 Setting up database directory..."
mkdir -p "$PROJECT_DIR/db"
chmod 777 "$PROJECT_DIR/db"

# 4. Make launcher executable and create symbolic link
echo "🔗 Setting up terminal launcher..."
chmod +x "$LAUNCHER_PATH"

mkdir -p "$BIN_DIR"
ln -sf "$LAUNCHER_PATH" "$SYMLINK_PATH"
echo "  ✓ Symlink created: $SYMLINK_PATH -> $LAUNCHER_PATH"

# 5. Register MCP Servers in detected CLIs and Editors
echo "🔌 Registering Simbad MCP server in detected platforms..."

# Register in Claude Code
if command -v claude &> /dev/null; then
    echo "🤖 Claude Code detected. Registering Simbad..."
    claude mcp add simbad -- docker run -i --rm --network=host -v "$PROJECT_DIR/db:/app/db" simbad > /dev/null 2>&1 || true
    echo "  ✓ Registered in Claude Code."
fi

# Register in OpenCode
OPENCODE_CONFIG="$HOME/.config/opencode/opencode.json"
if [ -f "$OPENCODE_CONFIG" ]; then
    echo "🖥️  OpenCode detected. Registering Simbad..."
    python3 -c "
import json, os
path = '$OPENCODE_CONFIG'
try:
    with open(path, 'r') as f:
        data = json.load(f)
except Exception:
    data = {}

if 'mcp' not in data:
    data['mcp'] = {}

data['mcp']['simbad-bridge'] = {
    'enabled': True,
    'type': 'local',
    'command': [
        'docker',
        'run',
        '-i',
        '--rm',
        '--network=host',
        '-v',
        '$PROJECT_DIR/db:/app/db',
        'simbad'
    ]
}

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
"
    echo "  ✓ Registered in OpenCode configuration."
fi

# Register in Claude Desktop
CLAUDE_DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
if [ -d "$HOME/.config/Claude" ] || [ -f "$CLAUDE_DESKTOP_CONFIG" ]; then
    echo "🎨 Claude Desktop folder detected. Registering Simbad..."
    mkdir -p "$HOME/.config/Claude"
    python3 -c "
import json, os
path = '$CLAUDE_DESKTOP_CONFIG'
try:
    with open(path, 'r') as f:
        data = json.load(f)
except Exception:
    data = {}

if 'mcpServers' not in data:
    data['mcpServers'] = {}

data['mcpServers']['simbad'] = {
    'command': 'docker',
    'args': [
        'run',
        '-i',
        '--rm',
        '--network=host',
        '-v',
        '$PROJECT_DIR/db:/app/db',
        'simbad'
    ]
}

with open(path, 'w') as f:
    json.dump(data, f, indent=2)
"
    echo "  ✓ Registered in Claude Desktop configuration."
fi

# 6. Check PATH variable
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "⚠️  Note: $BIN_DIR is not in your PATH environment variable."
    echo "  To run Simbad globally, add this to your ~/.bashrc or ~/.zshrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "🎉 Simbad (con Obscura) ha sido instalado exitosamente!"
echo "   Run 'simbad' in your terminal to start."
echo ""
echo "📌 NOTA: Ya no necesitas Chrome/Brave/Chromium."
echo "   Obscura (headless browser en Rust) corre dentro del contenedor."
echo "   Las sesiones (LinkedIn, WhatsApp, ChatGPT) persisten mientras"
echo "   el contenedor esté vivo. Para persistencia entre reinicios,"
echo "   se necesita configurar guardado de cookies."
echo ""
