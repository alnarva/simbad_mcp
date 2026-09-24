# Simbad MCP Server 🌐🤖

**El puente entre tu agente IA e internet.**

Simbad es un servidor MCP (Model Context Protocol) que convierte a cualquier LLM en un agente con capacidad de navegar, buscar, chatear, publicar y recordar — todo desde el navegador.

## ¿Qué hace?

Imagina un asistente que pueda:

- **Chatear por WhatsApp Business** — leer mensajes, buscar contactos, responder conversaciones
- **Publicar en LinkedIn** — escribir posts, comentar en tu feed, interactuar con tu red
- **Navegar la web** — buscar en DuckDuckGo, leer páginas, extraer contenido limpio (sin ads ni scripts)
- **Recordar** — dos memorias semánticas persistentes:
  - 🐋 **Pacífico** — recuerda conversaciones pasadas, decisiones de proyecto, historial de CLIs
  - 🌊 **Atlántico** — recuerda documentación web, páginas visitadas, resultados de búsqueda
- **Preguntarle a ChatGPT** — enviar prompts directamente a ChatGPT y obtener respuestas
- **Automatizar el navegador** — scroll, esperar elementos, sincronizar contenido, diagnosticar errores

## ¿Cómo funciona?

Simbad pilotea **dos motores de navegador** en paralelo:

| Motor | Puerto | Para qué |
|-------|--------|----------|
| **Obscura** 🕵️ | `:9222` | Modo stealth. LinkedIn, scraping, búsquedas web, ChatGPT. Corre en Docker. |
| **Chromium** 🧭 | `:9227` | Chrome headless real en el host. Exclusivo para WhatsApp Business. |

Ambos se orquestan desde un solo servidor MCP SSE en `http://localhost:9861/sse`.

## Memoria dual con Spike

Simbad usa **Spike** (Ollama + `nomic-embed-text`) para generar embeddings y guardar todo en SQLite con WAL mode:

- `simbad_browser.db` → **Atlántico**: conocimiento web (docs, páginas, búsquedas)
- `simbad_chats.db` → **Pacífico**: contexto conversacional (chats, decisiones, CLI logs)

GPU (NVIDIA) si está disponible, CPU si no.

## 20 herramientas MCP

| Herramienta | Descripción |
|-------------|-------------|
| `set_browser_engine` | Cambia entre Obscura y Chromium |
| `read_whatsapp_messages` | Lee mensajes de WhatsApp Business |
| `send_whatsapp_message` | Envía mensajes por WhatsApp |
| `get_feed_posts` | Escanea feed de LinkedIn |
| `comment_on_post` | Comenta en LinkedIn |
| `publish_linkedin_post` | Publica post en LinkedIn |
| `list_linkedin_conversations` | Lista las conversaciones del chat de LinkedIn |
| `read_linkedin_messages` | Lee los mensajes de un chat de LinkedIn |
| `send_linkedin_message` | Envía un mensaje de chat a un contacto o perfil de LinkedIn |
| `send_linkedin_connection_requests` | Envía solicitudes de conexión a perfiles sugeridos ("Gente que podrías conocer") |
| `send_linkedin_profile_connection` | Envía solicitud de conexión a un perfil específico de LinkedIn (con o sin nota) |
| `search_web` | Busca en DuckDuckGo |
| `read_webpage` | Extrae contenido de una URL |
| `ask_chatgpt` | Consulta a ChatGPT |
| `search_browser_history` | Busca en Atlántico (memoria web) |
| `search_chat_history` | Busca en Pacífico (memoria conversacional) |
| `semantic_search` | Búsqueda semántica en memoria del navegador |
| `scroll_page` | Desplaza la página activa |
| `sync_page_content` | Indexa contenido visible en Atlántico |
| `get_browser_logs` | Diagnostica errores del navegador |
| `wait_for_condition` | Espera a que aparezca un elemento |
| `open_in_browser` | Abre URL en nueva pestaña |

## stack

```
🧠 Ollama/Spike (embeddings)
🌐 Obscura (stealth browser, Docker)
🌐 Chromium (WhatsApp host)
🗄️ SQLite WAL (memoria dual)
🔌 FastMCP (SSE server)
```

## Origen del nombre

Simbad — como el marino de *Las mil y una noches* — navega entre dos mundos: el digital (browsers, APIs, memoria) y el del agente (LLM, decisiones, acciones). Es el vehículo que lleva la inteligencia artificial a explorar internet.
