FROM python:3.12-slim

WORKDIR /app

# ============================================================
# 1. Instalar dependencias del sistema (curl para descargar Obscura)
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# 2. Instalar dependencias Python (Playwright + MCP)
#    Playwright se usa solo como cliente CDP (connect_over_cdp),
#    no necesita descargar browsers.
# ============================================================
RUN pip install --no-cache-dir playwright mcp pillow qrcode

# ============================================================
# 3. Descargar Obscura (headless browser en Rust)
#    Drop-in replacement for headless Chromium, compatible con CDP
# ============================================================
RUN mkdir -p /app/bin && \
    curl -L -o /tmp/obscura.tar.gz \
    https://github.com/h4ckf0r0day/obscura/releases/latest/download/obscura-x86_64-linux.tar.gz && \
    tar xzf /tmp/obscura.tar.gz -C /app/bin/ && \
    rm /tmp/obscura.tar.gz && \
    chmod +x /app/bin/obscura /app/bin/obscura-worker

# ============================================================
# 4. Copiar código de Simbad y entrypoint
# ============================================================
COPY simbad_mcp.py entrypoint.sh ./

# ============================================================
# 5. Entrypoint: arranca Obscura como daemon, luego Simbad MCP
# ============================================================
ENTRYPOINT ["/app/entrypoint.sh"]
CMD []
