---
name: simbad-mcp
description: >
  Complete reference for the Simbad MCP server — 17 tools for WhatsApp, LinkedIn, web scraping, semantic memory, and browser automation via Obscura/Chromium.
---

# Simbad MCP Server — Skill Reference

## Connection

```bash
# Start the Simbad orchestrator (browsers + MCP SSE server)
./simbad

# AI CLIs connect to: http://localhost:9861/sse
```

## Browser Engines

| Engine | Port | Purpose |
|--------|------|---------|
| `obscura` | 9222 | Default. LinkedIn, Facebook, scraping, browsing. 1 sola instancia en Docker (`--workers 1`). |
| `chromium` | 9227 | **Only for WhatsApp Web.** Chromium headless real. Se auto-inicia con perfil persistente. |

Call `set_browser_engine("chromium")` antes de operaciones WhatsApp.
**Chromium se auto-inicia** vía `ensure_chromium()` con:
- `subprocess.DEVNULL` → sin ruido
- `--disk-cache-size=0 --disable-cache` → sin acumular basura
- `_prune_chromium_cache()` al inicio → limpia lo ya acumulado
- Perfil persistente en `whatsapp-auth/db/` → sesión WhatsApp sobrevive restarts

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
- Usa Spike (Ollama) para verificación semántica del contacto.

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
- `amount`: píxeles a scrollear (default 500).
- En WhatsApp scrollea el panel de chat activo.

### 11. `sync_page_content`
```python
sync_page_content() -> str
```
Extrae el contenido visible de la página actual, genera embeddings con Spike, y lo guarda en la base de datos semántica.

### 12. `get_browser_logs`
```python
get_browser_logs(duration_seconds: int = 5) -> str
```
Escucha errores de consola y peticiones de red fallidas en la pestaña activa. Útil para debugging sin activar detección antibot.

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
Escanea el feed de LinkedIn y devuelve posts NO comentados hoy. Usar antes de `comment_on_post`.

### 16. `comment_on_post`
```python
comment_on_post(author_name: str, comment_text: str) -> str
```
Comenta en el post de LinkedIn del autor indicado. Evita duplicados chequeando la base de datos local.

### 17. `publish_linkedin_post`
```python
publish_linkedin_post(content: str) -> str
```
Crea y publica un post en LinkedIn. Maneja Shadow DOM y pantalla de confirmación.

---

## Memory Databases

| Base | Nombre | Contenido | Tool |
|------|--------|-----------|------|
| **Atlántico** | `simbad_browser.db` | Docs, web scraping, búsquedas | `search_browser_history`, `semantic_search` |
| **Pacífico** | `simbad_chats.db` | Chats, decisiones, WhatsApp, CLI | `search_chat_history` |

Se pueblan automáticamente al usar `sync_page_content()` o al leer WhatsApp.

---

## LinkedIn Workflow

1. `get_feed_posts(limit=5)` — obtén posts sin comentar
2. Redacta comentario específico para cada post
3. `comment_on_post(author_name="...", comment_text="...")` — comenta

## WhatsApp Workflow

1. `set_browser_engine("chromium")` — cambia a Chromium (obligatorio)
2. `read_whatsapp_messages()` — lista chats
3. `read_whatsapp_messages(contact="Nombre")` — lee historial
4. `send_whatsapp_message(contact="Nombre", message="texto")` — envía

## Scrolling & Sync Loop

Para páginas largas o chats extensos:
1. `sync_page_content()` → indexa lo visible
2. `search_browser_history(query)` → busca
3. Si falta: `scroll_page("up", 500)` → `sync_page_content()` → buscar otra vez
4. Al terminar: `scroll_page("down", N)` para restaurar vista
