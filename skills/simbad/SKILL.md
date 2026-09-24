---
name: simbad-mcp
description: >
  Simbad is the internet gateway for AI agents — an MCP server that pilots two browser engines (Obscura stealth + Chromium) to give LLMs eyes, hands, and memory on the web. Navega, busca, scrollea, chatea por WhatsApp Business, publica en LinkedIn, Facebook y portales inmobiliarios, llena formularios, extrae páginas, y recuerda todo en dos bases semánticas (Atlántico + Pacífico) vía Spike embeddings. 27 tools todo-en-uno.
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
| `obscura` | 9222 | **Scraping, búsqueda web, ChatGPT.** Sin sesión de usuario. Corre en Docker. |
| `chromium` | 9227 | **Todo lo que requiera login.** LinkedIn, Facebook, WhatsApp Web, y cualquier sitio con sesión persistente. Chromium real headless en el host. Sesiones persistentes en `whatsapp-auth/db/`. |

- Usa `set_browser_engine("chromium")` **siempre** que necesites acceder a un sitio con sesión iniciada (LinkedIn, Facebook, WhatsApp, etc.).
- Las sesiones en Chromium sobreviven reinicios si ya fueron autenticadas.
- Para WhatsApp: si hay que re-autenticar, usar `simbad> whatsapp` desde el prompt interactivo.
- Para el resto (Facebook, LinkedIn, etc.): abre las URLs con `open_in_browser()` e inicia sesión manualmente la primera vez; la sesión se mantendrá.

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

## Tool Reference (27 tools)

### 1. `set_browser_engine`
```python
set_browser_engine(engine: str) -> str
```
Cambia entre `"obscura"` (9222, default) y `"chromium"` (9227, cualquier sitio con login: LinkedIn, Facebook, WhatsApp, portales inmobiliarios, etc.).

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
Escanea el feed de LinkedIn y devuelve posts NO comentados hoy. Cada item incluye `author`, `text` y `post_url` (permalink del post; puede venir vacío si no se encuentra).

### 16. `comment_on_post`
```python
comment_on_post(author_name: str, comment_text: str) -> str
```
Comenta en el post de LinkedIn del autor indicado. Evita duplicados chequeando la DB local.

⚠️ **REGLA MANDATORIA DE CALIDAD:** PROHIBIDO GENERAR COMENTARIOS GENÉRICOS, EN LOTE O CON PLANTILLAS REPETITIVAS (ej. *"Totalmente de acuerdo...", "Excelente aporte..."*). Cada comentario DEBE ser 100% único, leyendo y analizando a fondo el contenido específico del post del autor, citando sus ideas clave o aportando una perspectiva de valor única sobre su tema.

### 17. `publish_linkedin_post`
```python
publish_linkedin_post(content: str) -> str
```
Crea y publica un post en LinkedIn.

### 18. `clickup_api`
```python
clickup_api(api_key: str, method: str = "GET", path: str = "", data: str = "") -> str
```
Llama cualquier endpoint de ClickUp REST API.
- `api_key`: tu token `pk_...`
- `method`: GET, POST, PUT, DELETE
- `path`: ruta después de `/api/v2` (ej: `/team/{id}/space`, `/list/{id}/task`)
- `data`: JSON string opcional para POST/PUT

**Script standalone:** `clickup.py <api_key> <method> <path> [data_json]`

La URL base es `https://api.clickup.com/api/v2`. El tool acepta cualquier endpoint que exista en la API de ClickUp — no hay restricciones ni comandos fijos.

Para navegación visual con sesión persistente: `set_browser_engine("chromium")` + `open_in_browser("https://app.clickup.com/...")`.

---

### 19. `page_fill`
```python
page_fill(selector: str, text: str, engine: str = "chromium") -> str
```
Escribe texto en un campo de formulario identificado por selector CSS. Borra el contenido existente antes de escribir.
- `selector`: CSS selector del campo (ej. `#titulo`, `input[name="precio"]`)
- `text`: texto a escribir
- `engine`: `"chromium"` (default) o `"obscura"`

### 20. `page_click`
```python
page_click(selector: str, engine: str = "chromium") -> str
```
Hace click en un elemento de la página identificado por selector CSS. Espera hasta 5s a que el elemento aparezca.
- `selector`: CSS selector del elemento
- `engine`: `"chromium"` (default) o `"obscura"`

### 21. `page_evaluate`
```python
page_evaluate(js_code: str, engine: str = "chromium") -> str
```
**Uso exclusivo para Debugging e Inspección.** Ejecuta código JavaScript para depurar, verificar estado de la página, validar la presencia de selectores o inspeccionar elementos en el DOM. 

⚠️ **Regla:** No debe usarse para reemplazar acciones de usuario. Cada acción específica (enviar mensajes, agregar contactos, llenar formularios) debe ejecutarse utilizando su herramienta dedicada (`send_linkedin_profile_connection`, `send_linkedin_connection_requests`, `send_whatsapp_message`, `page_click`, etc.).

- `js_code`: código JS de diagnóstico (ej. `document.title`, inspección de nodos)
- `engine`: `"chromium"` (default) o `"obscura"`

### 22. `page_screenshot`
```python
page_screenshot(engine: str = "chromium") -> str
```
Toma una captura de pantalla de la página activa y la devuelve como imagen PNG. Útil para verificar estado o depurar selectores.

### 23. `page_get_text`
```python
page_get_text(selector: str, engine: str = "chromium") -> str
```
Obtiene el texto visible de un elemento identificado por selector CSS.
- `selector`: CSS selector

### 24. `page_upload`
```python
page_upload(selector: str, file_path: str, engine: str = "chromium") -> str
```
Sube un archivo (ej. foto) a un input de tipo file. La ruta debe ser absoluta en el host.
- `selector`: CSS selector del `input[type=file]`
- `file_path`: ruta absoluta al archivo (ej. `/home/alnar/foto.jpg`)

### 25. `page_select_option`
```python
page_select_option(selector: str, value: str, engine: str = "chromium") -> str
```
Selecciona una opción en un elemento `<select>` por su atributo `value`.
- `selector`: CSS selector del `<select>`
- `value`: valor de la opción a seleccionar

### 26. `get_linkedin_managed_pages`
```python
get_linkedin_managed_pages() -> str
```
Extrae **todas las páginas de empresa de LinkedIn gestionadas por el perfil logueado**, sin importar qué cuenta esté activa. Navega al feed, localiza la sección "Gestionar (N)", expande "Mostrar todo" y "Cargar más", y devuelve nombre + URL de cada página en JSON estructurado.

- Retorna: `{"declaredCount": N, "actualCount": N, "companies": [{"name": "...", "url": "..."}]}`
- ⚠️ Requiere `set_browser_engine("chromium")` y sesión iniciada en LinkedIn.
- Si no hay páginas gestionadas o no hay sesión, devuelve `error` + `hint`.

### 27. `publish_linkedin_company_post`
```python
publish_linkedin_company_post(company_url: str, content: str, linkedin_media_path: str = "") -> str
```
Crea y publica un post en una **página de empresa** de LinkedIn. Soporta texto, imágenes y vídeos.

- `company_url`: URL de la página (ej. `"https://www.linkedin.com/company/aillu-app/"`)
- `content`: Texto del post
- `linkedin_media_path`: (Opcional) Ruta absoluta al archivo dentro del contenedor (ej. `"/app/media/video.mp4"`). Si se omite, publica solo texto.
- Para vídeos: navega a Publicaciones de la página → sube el archivo → espera procesamiento → publica.
- LinkedIn redirige automáticamente al panel de admin si el usuario es administrador.
- ⚠️ Requiere `set_browser_engine("chromium")`, sesión iniciada y permisos de admin en la página.
- **Carpeta de media:** `/app/media/` (montada desde `~/Projects/MCP_SIMBAD/media/`).

### 28. `send_linkedin_connection_requests`
```python
send_linkedin_connection_requests(limit: int = 5, keywords: str = "") -> str
```
Navega a la sección de sugerencias de contactos ("Gente que podrías conocer") en LinkedIn (`https://www.linkedin.com/mynetwork/grow/`) y envía solicitudes de conexión automáticamente a perfiles recomendados.
- `limit`: cantidad máxima a enviar (default 5, máx 10 sugeridos por sesión).
- `keywords`: (Opcional) filtro por palabras clave separadas por comas (ej. `"IA, CEO, Gerente, Industrial"`).
- Evita duplicados guardando el registro en la base de datos local.

### 29. `send_linkedin_profile_connection`
```python
send_linkedin_profile_connection(profile_url: str, note: str = "") -> str
```
Navega al perfil específico de LinkedIn de una persona y envía una solicitud de conexión.
- `profile_url`: URL del perfil (ej. `"https://www.linkedin.com/in/nombre-apellido/"`).
- `note`: (Opcional) nota personalizada de hasta 300 caracteres.

### 30. `company_comment_on_post`
```python
company_comment_on_post(company_name: str, comment_text: str, author_name: str = "", post_url: str = "") -> str
```
Comenta una publicación de LinkedIn **USANDO LA IDENTIDAD DE UNA PÁGINA DE EMPRESA** administrada por el perfil logueado (cualquier compañía, no una fija).

- `company_name`: Nombre de la página de empresa desde la que comentar (ej. `"Aillu"`). Match por substring contra las páginas gestionadas.
- `comment_text`: Texto del comentario.
- `author_name`: (Opcional) Autor del post a localizar escaneando el feed.
- `post_url`: (Opcional) Permalink del post (alternativa a `author_name`). **Debe indicarse `author_name` o `post_url`.**

**Flujo:** resuelve la compañía contra `get_linkedin_managed_pages()` → obtiene el **Company ID numérico** → localiza el post (feed por autor o permalink) → navega al post con el parámetro **`?actorCompanyId=<CompanyId>`** (mecanismo oficial de LinkedIn para comentar como página de empresa) → **verifica que el comment box muestre el logo de la empresa** (guard anti-error, comparando avatar) → escribe, envía y registra.

**Nota técnica:** LinkedIn no tiene selector de identidad en el comment box del feed. El método soportado es la URL con `actorCompanyId`: al cargar el post con ese parámetro, la foto del comment box cambia al logo de la empresa y todo lo que se escriba en esa página se publica como la empresa. Requiere ser admin de la página. Los enlaces cortos `lnkd.in` se resuelven a su URL canónica primero (el redirect pierde el parámetro).

**⚠️ CAUSA RAÍZ CONOCIDA (corregida 26-ago-2026):** el envío del comentario usa **click REAL de Playwright** (`button:has-text("Comentar").last.click()`) y verifica la **publicación real** (el texto debe aparecer en el DOM de la página, no solo que la caja se vacíe). El click vía JS (`el.click()`) NO dispara los manejadores React de LinkedIn: la caja colapsaba sin publicar nada y la verificación de "caja vacía" daba un **falso positivo** (se registraba en DB sin haber comentado). No usar JS `.click()` para enviar comentarios.

**Rendimiento:** la lista de páginas gestionadas y el Company ID se **cachean por sesión** (no se re-navega en cada llamada). El flujo completo toma ~60-90s por comentario (navegación real + typing humano + verificación) — los timeouts de MCP (~30s) pueden cortarlo; ejecutar el flujo directo en el contenedor si se procesan varios comentarios seguidos.

**Anti-duplicados por identidad:** el comentario se registra con `platform='linkedin_company:<slug>'`, independiente del perfil personal. Comentar como página NO bloquea el mismo post desde el perfil personal, y viceversa.

**Errores útiles:** si la empresa no existe entre las gestionadas, devuelve JSON con las páginas disponibles; si el selector de identidad no aparece, devuelve error con los candidatos probados y causas probables.

- ⚠️ **REGLA MANDATORIA DE CALIDAD:** Prohibido comentarios genéricos, en lote o con plantillas repetitivas. Cada comentario debe ser 100% único y analizar la tesis específica del post del autor.
- ⚠️ Requiere `set_browser_engine("chromium")` y sesión iniciada en LinkedIn.

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

⚠️ **REGLA OBLIGATORIA:** Antes de generar cualquier comentario o publicación en LinkedIn, el agente DEBE leer y aplicar estrictamente las directrices del manual de estilo en [`manual_estilo.md`](file:///home/alnar/Projects/MCP_SIMBAD/manual_estilo.md) (tono profesional, sin jergas, conciso 1-3 líneas, 100% personalizado y sin comentarios genéricos).

1. `set_browser_engine("chromium")` — obligatorio (sesión persistente)
2. `get_linkedin_managed_pages()` — lista todas las páginas que gestionas
3. `get_feed_posts(limit=5)` — obtén posts sin comentar
4. **Consulta `manual_estilo.md` y redacta un comentario específico y 100% único por post** (PROHIBIDO usar plantillas, respuestas en lote o textos genéricos)
5. `comment_on_post(author_name="...", comment_text="...")` — comenta

**Publicar:**
- `publish_linkedin_post(content="...")` — publicar desde tu **perfil personal**
- `publish_linkedin_company_post(company_url="...", content="...")` — publicar desde una **página de empresa** que administras

**Comentar como página de empresa:**
1. `set_browser_engine("chromium")` — obligatorio (sesión persistente)
2. `get_linkedin_managed_pages()` — lista todas las páginas que gestionas
3. `get_feed_posts(limit=5)` — obtén posts sin comentar
4. **Consulta `manual_estilo.md` y redacta un comentario específico y 100% único por post**
5. `company_comment_on_post(company_name="Aillu", comment_text="...", author_name="Autor del post")` — comenta **como la página de empresa**
6. Alternativa directa: `company_comment_on_post(company_name="Aillu", comment_text="...", post_url="https://www.linkedin.com/feed/update/urn:li:activity:.../")`

### Facebook

1. `set_browser_engine("chromium")` — obligatorio (sesión persistente)
2. `open_in_browser("https://www.facebook.com/groups/...")` — abre el grupo
3. Usa `scroll_page()` + `read_webpage()` / `sync_page_content()` para explorar
4. La sesión se mantiene entre reinicios si ya fue autenticada

### Cualquier sitio con login (regla general)

1. `set_browser_engine("chromium")` — obligatorio
2. `open_in_browser("https://ejemplo.com")` — abre el sitio
3. Si es la primera vez: inicia sesión manualmente. La sesión queda persistente.
4. En usos posteriores: la sesión ya está activa.

### ClickUp

Tool MCP: `clickup_api(api_key, method, path, data)`
Script standalone: `clickup.py <api_key> <method> <path> [data_json]`

Ejemplos:
```
clickup_api(api_key, "GET", "/team")
clickup_api(api_key, "GET", "/team/90132707763/space")
clickup_api(api_key, "GET", "/list/1000500000001319/task")
clickup_api(api_key, "PUT", "/list/1000500000001319", '{"override_statuses":true,"statuses":[...]}')
clickup_api(api_key, "POST", "/list/1000500000001319/task", '{"name":"Test"}')
```

Navegación visual: `set_browser_engine("chromium")` + `open_in_browser("https://app.clickup.com/...")`

### Publicar en portales inmobiliarios

Requiere haber iniciado sesión **1 vez manualmente** en el portal desde Chromium.

#### BuscoCasita (gratis, sin límite)
```
1. set_browser_engine("chromium")
2. open_in_browser("https://peru.buscocasita.com/")
3. Iniciar sesión manualmente (solo la primera vez)
4. page_fill("#titulo", "Departamento en Miraflores - 3 dorm")
5. page_fill("#precio", "450000")
6. page_select_option("#tipo", "departamento")
7. page_select_option("#operacion", "venta")
8. page_upload("#fotos", "/home/almar/foto1.jpg")
9. page_click("#btn-publicar")
```

#### Facebook Groups
```
1. set_browser_engine("chromium")
2. open_in_browser("https://www.facebook.com/groups/ID_GRUPO/")
3. page_fill('[role="textbox"]', "¡Nuevo departamento en Miraflores!...")
4. page_click('[aria-label="Publicar"]')
```

#### Cualquier portal con formulario
```
1. set_browser_engine("chromium")          # si no se cambió antes
2. page_fill("#campo", "valor")            # llenar campos
3. page_select_option("#select", "opcion") # seleccionar dropdown
4. page_upload("#fotos", "ruta/foto.jpg")  # subir imágenes
5. page_click("#btn-enviar")               # enviar formulario
6. page_screenshot()                       # verificar resultado
```

### Scroll & Sync (páginas largas)

1. `sync_page_content()` → indexa lo visible
2. `search_browser_history(query)` → busca
3. Si falta contenido: `scroll_page("up", 500)` → `sync_page_content()` → buscar de nuevo
4. Al terminar: `scroll_page("down", N)` para restaurar vista
