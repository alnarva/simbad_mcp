---
name: simbad-mcp
description: >
  Complete reference for the Simbad MCP server — 17 tools for WhatsApp, LinkedIn, web scraping, semantic memory, and browser automation via Obscura/Chromium.
---

# Simbad MCP Server — Skill Reference

## Arrancar Simbad

```bash
cd ~/Projects/MCP_SIMBAD
./simbad
```

**Simbad es singleton**: si hay una instancia previa corriendo, la nueva la detecta y la cierra automáticamente antes de arrancar. No es necesario matar procesos manualmente.

### Secuencia de arranque

1. **Ollama + Spike** — modelo de embeddings sobre GPU (RTX 3050) si está disponible, CPU si no
2. **Chromium headless** (`:9227`) — con GPU por defecto (`--enable-gpu-rasterization`); cae a SwiftShader si no hay GPU
3. **sync_memories.py** — pobla Pacífico con historial de CLIs (timeout 90s, no bloqueante)
4. **Docker `simbad_run`** — lanza Obscura (`:9222`) + MCP SSE server (`:9861`)

### Endpoints activos

| Servicio | URL |
|----------|-----|
| MCP SSE  | `http://localhost:9861/sse` |
| Health   | `http://localhost:9861/health` |
| Obscura CDP | `http://localhost:9222` |
| Chromium CDP | `http://localhost:9227` |

---

## Browser Engines

| Engine | Puerto | Propósito |
|--------|--------|-----------|
| `obscura` | 9222 | **Default.** LinkedIn, scraping, búsqueda web. Corre en Docker. |
| `chromium` | 9227 | **Solo WhatsApp Web.** Chromium real headless en el host. Sesión persistente en `whatsapp-auth/db/`. |

- Usa `set_browser_engine("chromium")` antes de operaciones WhatsApp.
- La sesión de WhatsApp sobrevive reinicios si ya fue autenticada (no pide QR de nuevo).
- Si hay que re-autenticar WhatsApp: `simbad> whatsapp` desde el prompt interactivo.

### GPU en Chromium

El script detecta automáticamente:
- **GPU disponible** (`nvidia-smi` o `/dev/dri/renderD*`): lanza con `--enable-gpu-rasterization --enable-zero-copy`
- **Sin GPU**: lanza con `--disable-gpu --use-angle=swiftshader-webgl`

---

## Comandos interactivos (`simbad>`)

| Comando | Acción |
|---------|--------|
| `status` | Estado de Obscura, Chromium, MCP SSE y ambas DBs |
| `mcp` | URL y health del MCP SSE server |
| `whatsapp` | Verifica sesión WhatsApp o muestra QR para escanear |
| `help` | Lista de comandos |
| `exit` / `quit` | Cierra todo limpiamente (Docker, Chromium, Ollama si fue lanzado por simbad) |

---

## Tool Reference (17 tools)

### 1. `set_browser_engine`
```python
set_browser_engine(engine: str) -> str
```
Cambia entre `"obscura"` (9222, default) y `"chromium"` (9227, WhatsApp).

### 2. `ask_chatgpt`
```python
ask_chatgpt(prompt: str) -> str
```
Envía un prompt a la pestaña activa de ChatGPT en Obscura y devuelve la respuesta.

### 3. `semantic_search`
```python
semantic_search(query: str) -> str
```
Búsqueda semántica en la memoria del navegador (páginas scrolleadas, docs, resultados de búsqueda).

### 4. `search_browser_history`
```python
search_browser_history(query: str) -> str
```
**Atlántico.** Busca en documentación web, páginas scrapeadas, historial DuckDuckGo.

### 5. `search_chat_history`
```python
search_chat_history(query: str) -> str
```
**Pacífico.** Busca en conversaciones previas, decisiones de proyecto, logs de CLI, chats de WhatsApp.

### 6. `search_web`
```python
search_web(query: str) -> str
```
Busca en DuckDuckGo vía Obscura. Devuelve resumen limpio de resultados.

### 7. `read_webpage`
```python
read_webpage(url: str) -> str
```
Navega a una URL y extrae texto limpio (sin ads, scripts, footers).

### 8. `read_whatsapp_messages`
```python
read_whatsapp_messages(contact: str = None, limit: int = 15) -> str
```
- Sin `contact`: lista los chats recientes.
- Con `contact`: busca el contacto, lo abre, extrae hasta `limit` mensajes.
- ⚠️ Requiere `set_browser_engine("chromium")` primero.

### 9. `send_whatsapp_message`
```python
send_whatsapp_message(contact: str, message: str) -> str
```
Envía un mensaje a un contacto o grupo de WhatsApp.
- ⚠️ Requiere `set_browser_engine("chromium")`.

### 10. `scroll_page`
```python
scroll_page(direction: str, amount: int = 500) -> str
```
- `direction`: `"up"` o `"down"`.
- `amount`: píxeles (default 500).

### 11. `sync_page_content`
```python
sync_page_content() -> str
```
Extrae el contenido visible de la página actual, genera embeddings con Spike, y lo guarda en la base de datos semántica (Atlántico).

### 12. `get_browser_logs`
```python
get_browser_logs(duration_seconds: int = 5) -> str
```
Escucha errores de consola y peticiones de red fallidas. Útil para debugging.

### 13. `wait_for_condition`
```python
wait_for_condition(selector: str, timeout_ms: int = 5000) -> str
```
Espera a que un elemento (CSS selector, XPath, o data-testid) aparezca en la página activa.

### 14. `open_in_browser`
```python
open_in_browser(url: str) -> str
```
Abre una URL en una nueva pestaña del navegador activo.

### 15. `get_feed_posts`
```python
get_feed_posts(limit: int = 5) -> str
```
Escanea el feed de LinkedIn y devuelve posts NO comentados hoy.

### 16. `comment_on_post`
```python
comment_on_post(author_name: str, comment_text: str) -> str
```
Comenta en el post de LinkedIn del autor indicado. Evita duplicados chequeando la DB local.

### 17. `publish_linkedin_post`
```python
publish_linkedin_post(content: str) -> str
```
Crea y publica un post en LinkedIn.

---

## Bases de datos de memoria

| Base | Archivo | Tabla SQLite | Contenido | Tools |
|------|---------|--------------|-----------|-------|
| **Pacífico** | `simbad_chats.db` | `chats_context` | Chats, decisiones, historial de CLIs (Claude, Antigravity, OpenCode), WhatsApp | `search_chat_history` |
| **Atlántico** | `simbad_browser.db` | `web_context` | Docs, web scraping, búsquedas DuckDuckGo | `search_browser_history`, `semantic_search` |

- Ambas usan **WAL mode** (`PRAGMA journal_mode=WAL`) — permiten lectura concurrente mientras se escribe.
- Pacífico se puebla automáticamente en cada arranque vía `sync_memories.py` (timeout 90s).
- Atlántico se puebla al usar `sync_page_content()`.
- Los embeddings los genera **Spike** (modelo Ollama sobre `nomic-embed-text`), corriendo en GPU si está disponible.

---

## Singleton y PID file

- Al arrancar, `simbad` escribe su PID en `/tmp/simbad.pid`.
- Si ya existe un PID vivo: para `docker simbad_run`, mata el grupo de procesos anterior (incluye Chromium), espera 5s, SIGKILL si sigue vivo.
- Al salir limpiamente (`exit`, Ctrl+C), se elimina `/tmp/simbad.pid`.
- Procesos huérfanos de `sync_memories.py` se matan siempre antes del sync.

---

## Workflows

### WhatsApp

1. `set_browser_engine("chromium")` — obligatorio
2. `read_whatsapp_messages()` — lista chats recientes
3. `read_whatsapp_messages(contact="Nombre")` — lee historial
4. `send_whatsapp_message(contact="Nombre", message="texto")` — envía

### LinkedIn

1. `get_feed_posts(limit=5)` — obtén posts sin comentar
2. Redacta comentario específico
3. `comment_on_post(author_name="...", comment_text="...")` — comenta

### Scroll & Sync (páginas largas)

1. `sync_page_content()` → indexa lo visible
2. `search_browser_history(query)` → busca
3. Si falta contenido: `scroll_page("up", 500)` → `sync_page_content()` → buscar de nuevo
4. Al terminar: `scroll_page("down", N)` para restaurar vista
