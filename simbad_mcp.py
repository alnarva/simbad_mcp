import asyncio
import os
import sqlite3
import json
import urllib.request
import random
import hashlib
import math
import time
import tempfile
from datetime import datetime, timedelta
from mcp.server.fastmcp import FastMCP, Image as MCPImage
import subprocess
import signal
from playwright.async_api import async_playwright
import builtins
import sys

_original_print = builtins.print
def _stderr_print(*args, **kwargs):
    kwargs["file"] = sys.stderr
    _original_print(*args, **kwargs)
    sys.stderr.flush()
builtins.print = _stderr_print

# Global: QR screenshot para servir en los MCP tools
_qr_screenshot_bytes = None

# Obscura single-instance manager
_obscura_process = None
_obscura_spawned_by_us = False
OBSCURA_PORT = 9222
CHROMIUM_PORT = 9227

def _check_cdp(port):
    """Check if a CDP endpoint is responding on the given port."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False

def _db_dir():
    """Ruta base para datos persistentes (screenshots, DBs). Docker vs host."""
    if os.path.exists("/app/db"):
        return "/app/db"
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db")
    os.makedirs(d, exist_ok=True)
    return d

def _get_obscura_bin():
    """Locate the Obscura binary file."""
    for path in ["/app/bin/obscura", "./bin/obscura", "../bin/obscura", "obscura"]:
        if os.path.exists(path):
            return path
    return None

def _find_chromium_bin():
    """Locate the Chromium binary — solo Chromium real. Nada de Google Chrome."""
    for path in ["/usr/bin/chromium", "chromium", "chromium-browser"]:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and "Chromium" in result.stdout:
                return path
        except Exception:
            continue
    return None

def _prune_chromium_cache(profile_dir):
    """Elimina basura acumulada del perfil Chromium preservando cookies/sesión.
    
    Elimina cachés a nivel raíz y dentro de Default/, conservando
    cookies, localStorage, IndexedDB de WhatsApp y datos de inicio de sesión.
    
    Las flags --disable-cache --disk-cache-size=0 evitan que vuelva a crecer.
    """
    import fnmatch, shutil
    
    if not os.path.isdir(profile_dir):
        return
    
    keep_root = {"Default", "Local State", "cookies.json", "FirstPartySetsPreloaded",
                 "SSLErrorAssistant", "CertificateRevocation", "PKIMetadata",
                 "Last Version", "Variations", "obscura-profile"}
    
    # Directorios de caché a nivel raíz (seguro eliminar)
    cache_root_dirs = {"Cache", "Code Cache", "GPUCache", "DawnCache",
                       "Component Updates", "component_crx_cache", "extensions_crx_cache",
                       "Safe Browsing", "OptimizationHints", "OptimizationGuide",
                       "OnDeviceHeadSuggestModel", "OptGuideOnDeviceClassifierModel",
                       "OptGuideOnDeviceModel", "segmentation_platform",
                       "Crowd Deny", "Subresource Filter", "hyphen-data",
                       "MEIPreload", "FileTypePolicies", "TrustTokenKeyCommitments",
                       "WasmTtsEngine", "ZxcvbnData", "OriginTrials",
                       "PrivacySandboxAttestationsPreloaded", "SafetyTips",
                       "Dictionaries", "GraphiteDawnCache", "ActorSafetyLists",
                       "AmountExtractionHeuristicRegexes", "CaptionProviders",
                       "NativeMessagingHosts", "WidevineCdm",
                       "optimization_guide_model_store"}
    
    cache_root_glob = {"BrowserMetrics*", "first_party_sets.db*",
                       "DevToolsActivePort", "Singleton*"}
    
    # Subdirectorios dentro de Default/ que son caché (seguro eliminar)
    cache_inside_default = {"Cache", "Code Cache", "GPUCache", "DawnCache",
                            "Service Worker/CacheStorage",
                            "File System", "extensions",
                            "Session Storage", "Local Storage",  # WA usa localStorage en Default
                            "databases", "blob_storage", "shared_proto_db",
                            "IndexedDB/https_web.whatsapp.com_0.indexeddb.leveldb"}
    # NOTA: Local Storage y Session Storage se eliminan, pero WA usa localStorage
    # que está en Default/Local Storage/. Eso haría perder sesión.
    # Por eso NO eliminamos esos - solo los de arriba.
    
    # Limpiar raíz
    for entry in os.listdir(profile_dir):
        path = os.path.join(profile_dir, entry)
        if entry in keep_root:
            continue
        if entry in cache_root_dirs and os.path.isdir(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
            continue
        for pattern in cache_root_glob:
            if fnmatch.fnmatch(entry, pattern):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        os.unlink(path)
                except Exception:
                    pass
                break
    
    # Limpiar subcachés dentro de Default/ (Cache, Code Cache, GPUCache, etc.)
    # PERO preservar Local Storage, Session Storage, Cookies, IndexedDB
    default_dir = os.path.join(profile_dir, "Default")
    if not os.path.isdir(default_dir):
        return
    
    # Preservar estos dentro de Default/
    keep_in_default = {"Cookies", "Cookies-journal", "Local Storage", 
                       "Session Storage", "Extensions", "Web Data",
                       "Login Data", "Login Data-journal", "Bookmarks",
                       "Bookmarks.bak", "Preferences", "Secure Preferences",
                       "Current Session", "Current Tabs", "Last Session",
                       "Last Tabs", "History", "History-journal",
                       "Favicons", "Favicons-journal", "Top Sites",
                       "Shortcuts", "Shortcuts-journal", "Storage",
                       "Sync Data", "README", "Network Action Predictor",
                       "Network Persistent State", "TransportSecurity",
                       "Trusted Vault", "FileTypePolicies"}
    
    for entry in os.listdir(default_dir):
        if entry in keep_in_default:
            continue
        path = os.path.join(default_dir, entry)
        if entry in {"Cache", "Code Cache", "GPUCache", "DawnCache",
                     "databases", "blob_storage", "shared_proto_db",
                     "File System"} and os.path.isdir(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
        elif entry.startswith("extensions_") and os.path.isdir(path):
            # Extensiones cacheadas (seguro eliminar, se descargan de nuevo)
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
        elif os.path.isfile(path):
            ext = os.path.splitext(entry)[1]
            if ext in {".pma", ".log", ".old", ".tmp", ".journal"}:
                try:
                    os.unlink(path)
                except Exception:
                    pass

def _rmtree(path):
    """shutil.rmtree silencioso."""
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass

def ensure_chromium():
    """Ensure Chromium is running headlessly on CHROMIUM_PORT (9227).
    
    - Idempotent: if Chromium is already listening on 9227, returns True.
    - Auto-starts Chromium in headless mode with all output silenced (DEVNULL).
    - Uses the project's whatsapp-auth/db as persistent user-data-dir (perfil aislado de cualquier Chrome personal).
    - Never spawns more than one Chromium instance.
    """
    global _chromium_process
    
    # Already running? Done.
    if _check_cdp(CHROMIUM_PORT):
        return True
    
    # Already spawned by us and alive? Done.
    if _chromium_process is not None and _chromium_process.poll() is None:
        if _check_cdp(CHROMIUM_PORT):
            return True
        # Dead; clean up
        try:
            _chromium_process.kill()
        except Exception:
            pass
        _chromium_process = None
    
    chromium_bin = _find_chromium_bin()
    if not chromium_bin:
        return False
    
    # Use the project's persistent profile for WhatsApp auth
    script_dir = os.path.dirname(os.path.abspath(__file__))
    user_data_dir = os.path.join(script_dir, "whatsapp-auth", "db")
    os.makedirs(user_data_dir, exist_ok=True)
    
    # Remove stale lock files from previous Chromium instances
    for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lock_path = os.path.join(user_data_dir, lock_file)
        if os.path.exists(lock_path):
            try:
                os.unlink(lock_path)
            except Exception:
                pass
    
    # Limpiar basura acumulada del perfil antes de iniciar
    _prune_chromium_cache(user_data_dir)
    
    try:
        _chromium_process = subprocess.Popen(
             [chromium_bin, "--headless",
             f"--remote-debugging-port={CHROMIUM_PORT}",
             f"--user-data-dir={user_data_dir}",
             "--disable-gpu", "--disable-software-rasterizer",
             "--no-first-run", "--no-default-browser-check",
             "--noerrdialogs",
             "--ozone-platform=headless",
             "--ozone-override-screen-size=800,600",
             "--use-angle=swiftshader-webgl",
             "--disable-sync", "--disable-breakpad",
             "--disk-cache-size=0", "--media-cache-size=0",
             "--disable-cache", "--disable-application-cache",
             "--aggressive-cache-discard",
             # User-Agent sin "Headless" para que WhatsApp no bloquee
             "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        # Wait up to 10s for CDP to become available
        for _ in range(20):
            if _check_cdp(CHROMIUM_PORT):
                return True
            time.sleep(0.5)
        # Timed out; clean up
        try:
            _chromium_process.kill()
        except Exception:
            pass
        _chromium_process = None
        return False
    except Exception:
        _chromium_process = None
        return False

# Global: shared Chromium process handle
_chromium_process = None

def ensure_obscura():
    """Ensure the active browser engine is running and silenced.
    
    - Engine 'obscura' → starts/stops Obscura on port 9222 (idempotent).
    - Engine 'chromium'  → delegates to ensure_chromium() (Chromium headless en 9227).
    - All browser output goes to DEVNULL — no terminal noise.
    - Never spawns duplicate instances.
    """
    global _obscura_process, _obscura_spawned_by_us, CURRENT_ENGINE
    
    # For Chromium engine, delegate to ensure_chromium()
    if CURRENT_ENGINE == "chromium":
        return ensure_chromium()
    
    # If we already spawned it, verify it's still alive
    if _obscura_spawned_by_us:
        if _check_cdp(OBSCURA_PORT):
            return True
        # Process died; clear flag so we respawn
        _obscura_spawned_by_us = False
        _obscura_process = None
    
    # Check if something is already listening on the Obscura port
    # (could be entrypoint.sh or a previous run)
    if _check_cdp(OBSCURA_PORT):
        return True
    
    # Find the Obscura binary
    obscura_bin = _get_obscura_bin()
    if not obscura_bin:
        return False
    
    # Kill any leftover Obscura process we previously spawned but lost track of
    if _obscura_process is not None and _obscura_process.poll() is None:
        try:
            _obscura_process.send_signal(signal.SIGTERM)
            _obscura_process.wait(timeout=3)
        except Exception:
            try:
                _obscura_process.kill()
            except Exception:
                pass
        _obscura_process = None
    
    # Spawn Obscura on the single fixed port
    try:
        _obscura_process = subprocess.Popen(
            [obscura_bin, "serve", "--port", str(OBSCURA_PORT), "--stealth"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        # Wait up to 10s for it to be ready
        for _ in range(20):
            if _check_cdp(OBSCURA_PORT):
                _obscura_spawned_by_us = True
                return True
            time.sleep(0.5)
        # Timed out; clean up
        try:
            _obscura_process.kill()
        except Exception:
            pass
        _obscura_process = None
        return False
    except Exception:
        _obscura_process = None
        return False

def stop_browsers():
    """Detiene browsers iniciados por Simbad (no toca entrypoint ni el Chromium del escritorio)."""
    global _obscura_process, _obscura_spawned_by_us, _chromium_process
    
    # Detener Obscura (si fue iniciado por ensure_obscura())
    if _obscura_spawned_by_us and _obscura_process is not None:
        try:
            _obscura_process.send_signal(signal.SIGTERM)
            _obscura_process.wait(timeout=5)
        except Exception:
            try:
                _obscura_process.kill()
            except Exception:
                pass
        _obscura_process = None
        _obscura_spawned_by_us = False
    
    # Detener Chromium headless (si fue iniciado por ensure_chromium())
    if _chromium_process is not None and _chromium_process.poll() is None:
        try:
            _chromium_process.send_signal(signal.SIGTERM)
            _chromium_process.wait(timeout=5)
        except Exception:
            try:
                _chromium_process.kill()
            except Exception:
                pass
        _chromium_process = None

async def human_type(element, text):
    for char in text:
        await element.type(char)
        await asyncio.sleep(random.randint(11, 54) / 1000.0)

def get_ollama_embedding(text):
    url = "http://localhost:11434/api/embeddings"
    data = {
        "model": "spike",
        "prompt": f"search_document: {text}"
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode("utf-8"))
            return res.get("embedding")
    except Exception as e:
        sys.stderr.write(f"Warning: Could not fetch embedding from Ollama: {e}\n")
        return None

def save_page_context(url, title, content):
    db_dir = _db_dir()
    os.makedirs(db_dir, exist_ok=True)
    
    # 1. Determine which database and table to use
    # Pacific (simbad_chats.db) is exclusively for terminal project CLIs
    is_project_cli = (
        url.startswith("claude-code://") or 
        url.startswith("antigravity://")
    )
    
    if is_project_cli:
        db_path = os.path.join(db_dir, "simbad_chats.db")
        table_name = "chats_context"
    else:
        # Web scraping, searches, WhatsApp, and ChatGPT browser tabs go to Atlantic
        db_path = os.path.join(db_dir, "simbad_browser.db")
        table_name = "web_context"
    
    # Validar table_name contra lista blanca para evitar SQL injection
    VALID_TABLES = {"chats_context", "web_context"}
    if table_name not in VALID_TABLES:
        print(f"Warning: Invalid table name '{table_name}' rejected.")
        return
        
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT,
                title TEXT,
                content TEXT,
                embedding TEXT,
                content_hash TEXT,
                timestamp TEXT
            )
        """)
        
        # Alter table if columns don't exist (backward compatibility)
        for col in ["embedding", "content_hash"]:
            try:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        
        # Check if embedding already exists for this content hash
        cursor.execute(f"SELECT embedding FROM {table_name} WHERE content_hash = ? AND embedding IS NOT NULL LIMIT 1", (content_hash,))
        row = cursor.fetchone()
        
        if row and row[0]:
            embedding_json = row[0]
            print(f"  ✓ Reusing cached embedding for content hash {content_hash[:8]}")
        else:
            embedding = get_ollama_embedding(content)
            embedding_json = json.dumps(embedding) if embedding else None
            
        cursor.execute(f"""
            INSERT INTO {table_name} (url, title, content, embedding, content_hash, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (url, title, content, embedding_json, content_hash, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        try:
            os.chmod(db_path, 0o666)
        except Exception:
            pass
        print(f"Context saved to {table_name}: '{title}' ({url})")
    except Exception as e:
        print(f"Warning: Could not save context to {table_name}: {e}")


mcp = FastMCP("Simbad")

CURRENT_ENGINE = "obscura"

def get_cdp_url():
    global CURRENT_ENGINE
    if CURRENT_ENGINE == "chromium":
        return os.environ.get("CHROMIUM_CDP_URL", f"http://127.0.0.1:{CHROMIUM_PORT}")
    return os.environ.get("BROWSER_CDP_URL", f"http://127.0.0.1:{OBSCURA_PORT}")

@mcp.tool()
async def set_browser_engine(engine: str) -> str:
    """
    Cambia el motor de navegación activo para los siguientes comandos de Simbad.
    Opciones:
      - 'obscura' (puerto 9222): El motor por defecto, ultra-rápido, modo stealth. Ideal para investigar, Facebook, LinkedIn, etc.
      - 'chromium' (puerto 9227): Chromium headless en el host. EXCLUSIVAMENTE para WhatsApp Web.
    """
    global CURRENT_ENGINE
    engine = engine.lower().strip()
    if engine not in ["obscura", "chromium"]:
        return "Error: motor inválido. Usa 'obscura' o 'chromium'."
    CURRENT_ENGINE = engine
    return f"Motor cambiado exitosamente a '{engine}'. Las siguientes acciones usarán este navegador."

async def run_chatgpt(context, prompt):
    pages = context.pages
    page = None
    for p in pages:
        if "chatgpt.com" in p.url:
            page = p
            break
            
    if not page:
        page = await context.new_page()
        await page.goto("https://chatgpt.com")
    else:
        pass
        
    try:
        await page.wait_for_selector("#prompt-textarea", timeout=10000)
        await page.fill("#prompt-textarea", prompt)
        await page.press("#prompt-textarea", "Enter")
        
        await asyncio.sleep(2)
        for _ in range(90):
            stop_button = await page.query_selector('[data-testid="stop-button"]')
            if not stop_button:
                break
            await asyncio.sleep(1)
            
        response_text = ""
        messages = await page.query_selector_all('div[data-message-author-role="assistant"]')
        if messages:
            response_text = await messages[-1].inner_text()
        else:
            articles = await page.query_selector_all('article')
            if articles:
                response_text = await articles[-1].inner_text()
                
        if response_text:
            title = await page.title()
            save_page_context(page.url, title or "ChatGPT Conversation", f"Prompt: {prompt}\n\nResponse:\n{response_text}")
            return response_text
            
        return "Error: Could not locate the response in the ChatGPT interface."
        
    except Exception as e:
        return f"Error interacting with ChatGPT tab: {e}"

@mcp.tool()
async def ask_chatgpt(prompt: str) -> str:
    """
    Send a prompt to the active ChatGPT tab in Obscura (port 9222) and retrieve the response.
    Obscura runs as a headless browser inside the Simbad container.
    """
    cdp_url = get_cdp_url()
    if not ensure_obscura():
        return (
            f"Error: Could not connect to Obscura on {cdp_url}.\n"
            "Please make sure Obscura is running (use --network=host with the Simbad container).\n"
        )
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return (
                f"Error: Could not connect to Obscura on {cdp_url}.\n"
                f"Details: {e}"
            )

        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found in the connected session."
        
        context = contexts[0]
        return await run_chatgpt(context, prompt)

async def _check_qr_and_capture(page):
    """Detecta QR de WhatsApp y captura screenshot en memoria.
    
    Returns: bytes PNG si hay QR, None si no."""
    global _qr_screenshot_bytes
    _qr_screenshot_bytes = None
    
    # Método 1: canvas QR visible
    try:
        await page.wait_for_selector('canvas[aria-label="Scan me!"]', timeout=3000)
        _qr_screenshot_bytes = await page.screenshot(type="png")
        return _qr_screenshot_bytes
    except Exception:
        pass
    
    # Método 2: data-ref en el HTML (WhatsApp aún cargando)
    try:
        content = await page.content()
        if "data-ref" in content:
            await asyncio.sleep(3)
            _qr_screenshot_bytes = await page.screenshot(type="png")
            return _qr_screenshot_bytes
    except Exception:
        pass
    
    return None

async def run_whatsapp(context, contact, message):
    js_click = """el => {
        const r = el.getBoundingClientRect();
        const opts = { bubbles: true, cancelable: true, view: window, clientX: r.left + r.width/2, clientY: r.top + r.height/2 };
        el.dispatchEvent(new MouseEvent('mousedown', opts));
        el.dispatchEvent(new MouseEvent('mouseup', opts));
        el.dispatchEvent(new MouseEvent('click', opts));
    }"""

    pages = context.pages
    page = None
    for p in pages:
        if "web.whatsapp.com" in p.url:
            page = p
            break
            
    if not page:
        page = await context.new_page()
        await page.goto("https://web.whatsapp.com")
    else:
        pass
        
    # First check if it's asking for a QR code
    qr_bytes = await _check_qr_and_capture(page)
    if qr_bytes:
        return qr_bytes

    try:
        # Wait for search box to be visible
        search_selector = 'input[aria-label="Buscar un chat o iniciar uno nuevo"], input[aria-label="Search or start new chat"], div[title="Buscar un chat o iniciar uno nuevo"], p.selectable-text, div[contenteditable="true"]'
        search_box = await page.wait_for_selector(search_selector, timeout=20000)
            
        # Focus and clear the search box
        await search_box.focus()
        await search_box.fill("")
        
        # Use our randomized human_type helper to simulate realistic keystrokes and avoid detection
        await human_type(search_box, contact)
        await asyncio.sleep(2)
        
        # Restrict contact selection to the sidebar panel
        clicked = False
        sidebar_selectors = ['#pane-side', '[data-testid="chat-list"]', 'div[role="grid"]']
        sidebar = None
        for sel in sidebar_selectors:
            try:
                sidebar = await page.wait_for_selector(sel, timeout=2000)
                if sidebar:
                    break
            except Exception:
                continue
                
        if sidebar:
            try:
                contact_selector = f'span[title="{contact}"]'
                contact_tile = await sidebar.wait_for_selector(contact_selector, timeout=3000)
                # Use DOM-level simulated mouse click to prevent OS mouse warping
                await contact_tile.evaluate(js_click)
                clicked = True
            except Exception:
                try:
                    contact_tile = await sidebar.locator('span').filter(has_text=contact).first
                    await contact_tile.evaluate(js_click)
                    clicked = True
                except Exception:
                    try:
                        # Fallback to the first search result in the sidebar list
                        first_chat = await sidebar.locator('div[role="row"], [data-testid="list-item"], ._ak72, ._ak73').first
                        await first_chat.evaluate(js_click)
                        clicked = True
                    except Exception as e:
                        print(f"Could not click first search result: {e}")
                        
        if not clicked:
            # Press Enter at element level
            await search_box.press("Enter")
            
        await asyncio.sleep(2)
        
        # Detector de coincidencia: Verificar que el chat abierto sea el correcto
        try:
            chat_title_elem = await page.wait_for_selector('#main header span[dir="auto"]', timeout=3000, state="attached")
            actual_chat_title = await chat_title_elem.text_content() if chat_title_elem else "Unknown"
            
            contact_lower = contact.lower()
            actual_lower = actual_chat_title.lower()
            
            emb1 = get_ollama_embedding(f"search_query: {contact_lower}")
            emb2 = get_ollama_embedding(f"search_query: {actual_lower}")
            
            if emb1 is not None and emb2 is not None:
                similarity = cosine_similarity(emb1, emb2)
                print(f"Spike semantic similarity: {similarity:.2f}")
                if similarity < 0.75:
                    return f"Error de seguridad abortado: El chat abierto ('{actual_chat_title}') no coincide con el contacto solicitado ('{contact}'). Similitud semántica muy baja ({similarity:.2f})."
            else:
                # Fallback: comparación directa cuando no hay Ollama
                if contact_lower not in actual_lower and actual_lower not in contact_lower:
                    return f"Error de seguridad: El chat abierto ('{actual_chat_title}') no coincide con el contacto solicitado ('{contact}')."
        except Exception as e:
            print(f"No se pudo verificar el título del chat: {e}")
        
        # Restrict input box selection to the main chat pane (#main) to avoid matching the search box
        input_box_selector = '#main [data-testid="conversation-compose-box-input"], #main footer div[contenteditable="true"][role="textbox"]'
        try:
            input_box = await page.wait_for_selector(input_box_selector, timeout=10000)
        except Exception as e:
            # Fallback to general input box selector if #main is structured differently
            input_box = await page.wait_for_selector('[data-testid="conversation-compose-box-input"], div[contenteditable="true"][role="textbox"]', timeout=5000)
        
        # Focus the input box
        await input_box.focus()
        
        # Use our randomized human_type helper to type the message naturally with randomized delays
        await human_type(input_box, message)
        await asyncio.sleep(1)
        
        # Send message by pressing Enter on the input box element
        await input_box.press("Enter")
        
        await asyncio.sleep(2)
        save_page_context(page.url, f"WhatsApp Chat with {contact}", f"Sent message: {message}")
        return f"Mensaje enviado con éxito a '{contact}'."
    except Exception as e:
        return f"Error interacting with WhatsApp Web: {e}"

async def run_read_webpage(context, url):
    page = await context.new_page()
    try:
        print(f"Navigating to {url}...")
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(2)  # Wait for lazy rendering
        
        title = await page.title()
        
        # Clean the DOM in-browser using JS to minimize data transfer and token cost
        clean_text = await page.evaluate("""() => {
            const toRemove = document.querySelectorAll('script, style, iframe, nav, footer, header, aside, noscript, .footer, .header, .sidebar, .ads, .ad');
            toRemove.forEach(el => el.remove());
            
            const mainContent = document.querySelector('article') || document.querySelector('main') || document.querySelector('[role="main"]') || document.body;
            
            // Clean consecutive white spaces and returns
            return mainContent.innerText.replace(/\\n\\s*\\n+/g, '\\n\\n').trim();
        }""")
        
        save_page_context(url, title, clean_text)
        
        # Return truncated clean content to keep tokens minimal
        return f"Title: {title}\nURL: {url}\n\nContent:\n{clean_text[:8000]}"
    except Exception as e:
        return f"Error reading webpage {url}: {e}"
    finally:
        await page.close()

async def run_search_web(context, query):
    page = await context.new_page()
    try:
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        print(f"Searching DuckDuckGo for: '{query}'...")
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        
        results = await page.evaluate("""() => {
            const list = [];
            const links = document.querySelectorAll('.web-result');
            links.forEach(el => {
                const titleEl = el.querySelector('.result__title a');
                const snippetEl = el.querySelector('.result__snippet');
                if (titleEl && snippetEl) {
                    list.push({
                        title: titleEl.innerText.trim(),
                        url: titleEl.href,
                        snippet: snippetEl.innerText.trim()
                    });
                }
            });
            return list.slice(0, 5); // Return top 5 results
        }""")
        
        formatted_results = []
        for i, res in enumerate(results):
            formatted_results.append(f"[{i+1}] {res['title']}\nURL: {res['url']}\nSnippet: {res['snippet']}\n")
            
        search_summary = "\n".join(formatted_results)
        save_page_context(url, f"Search: {query}", search_summary)
        
        return f"Search Results for '{query}':\n\n{search_summary}"
    except Exception as e:
        return f"Error performing web search: {e}"
    finally:
        await page.close()

def get_ollama_query_embedding(query):
    url = "http://localhost:11434/api/embeddings"
    data = {
        "model": "spike",
        "prompt": f"search_query: {query}"
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode("utf-8"))
            return res.get("embedding")
    except Exception as e:
        print(f"Error fetching query embedding: {e}")
        return None

def cosine_similarity(v1, v2):
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_a = sum(a * a for a in v1) ** 0.5
    norm_b = sum(b * b for b in v2) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)

async def run_semantic_search(context, query, db_name="simbad_browser.db", table_name="web_context"):
    query_vector = get_ollama_query_embedding(query)
    if not query_vector:
        return "Error: Could not generate query embedding. Make sure Ollama is running."
        
    db_path = os.path.join(_db_dir(), db_name)
    if not os.path.exists(db_path):
        return f"Database ({db_name}) is empty. No context available to search."
        
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(f"SELECT id, url, title, content, embedding, timestamp FROM {table_name}")
        rows = cursor.fetchall()
        conn.close()
        
        matches = []
        for row in rows:
            row_id, url, title, content, embedding_json, timestamp = row
            if not embedding_json:
                continue
            embedding = json.loads(embedding_json)
            score = cosine_similarity(query_vector, embedding)
            matches.append({
                "id": row_id,
                "url": url,
                "title": title,
                "content": content,
                "score": score,
                "timestamp": timestamp
            })
            
        # Sort by similarity score descending
        matches.sort(key=lambda x: x["score"], reverse=True)
        
        formatted_matches = []
        for i, match in enumerate(matches[:5]): # Top 5 semantic matches
            formatted_matches.append(
                f"[{i+1}] {match['title']} (Score: {match['score']:.4f})\n"
                f"URL: {match['url']}\n"
                f"Date: {match['timestamp']}\n"
                f"Excerpt: {match['content'][:400].strip()}...\n"
            )
            
        if not formatted_matches:
            return f"No semantic matches found in {table_name}."
            
        return f"Semantic search results in Simbad {table_name} for '{query}':\n\n" + "\n".join(formatted_matches)
    except Exception as e:
        return f"Error during semantic search: {e}"

@mcp.tool()
async def semantic_search(query: str) -> str:
    """
    Search semantically across Simbad's local browser context memory (scraped pages, documentation, search results).
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        return await run_semantic_search(context, query, db_name="simbad_browser.db", table_name="web_context")

@mcp.tool()
async def search_browser_history(query: str) -> str:
    """
    ATLANTIC DATABASE (BROWSER CONTEXT): Use this tool ONLY when searching for web documentation,
    scraped web pages, DuckDuckGo search history, or reference material. Searches semantically across
    Simbad's local browser memory.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        return await run_semantic_search(context, query, db_name="simbad_browser.db", table_name="web_context")

@mcp.tool()
async def search_chat_history(query: str) -> str:
    """
    PACIFIC DATABASE (MEMORY/PROJECT CONTEXT): Use this tool ONLY when searching for information about
    the active project, code implementations, past CLI conversations, user instructions, decisions, or chat logs.
    Searches semantically across Simbad's local chat memory.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        return await run_semantic_search(context, query, db_name="simbad_chats.db", table_name="chats_context")

@mcp.tool()
async def search_web(query: str) -> str:
    """
    Search the web for a query using DuckDuckGo via Obscura headless browser (port 9222).
    Returns a clean, token-efficient summary of the top search results.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        return await run_search_web(context, query)

@mcp.tool()
async def read_webpage(url: str) -> str:
    """
    Navigate to a webpage and extract its text content via Obscura headless browser (port 9222).
    Automatically strips ads, scripts, footers, headers, and returns clean, token-efficient content.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        return await run_read_webpage(context, url)

async def run_read_whatsapp_chats(context, limit=None):
    pages = context.pages
    page = None
    for p in pages:
        if "web.whatsapp.com" in p.url:
            page = p
            break

    if not page:
        page = await context.new_page()
        await page.goto("https://web.whatsapp.com")

    # Wait for chat list — try multiple selectors (WhatsApp Web vs Business)
    try:
        await page.wait_for_selector('#pane-side, div[aria-label="Lista de chats"], div[aria-label="Chat list"]', timeout=15000)
    except Exception:
        qr_bytes = await _check_qr_and_capture(page)
        if qr_bytes:
            return qr_bytes
        return "Error: Could not find chat list. WhatsApp might still be loading or the DOM has changed."

    try:
        # Try multiple selectors for chat rows
        chats = await page.query_selector_all('#pane-side div[role="row"], #pane-side div[role="listitem"]')
        if not chats:
            return "No recent chats found."

        if limit is not None:
            chats = chats[:limit]

        result = []
        for i, chat in enumerate(chats):
            text = await chat.inner_text()
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            if lines:
                result.append(f"{i+1}. " + " | ".join(lines))

        if result:
            chats_text = "\n".join(result)
            # Guardar con Spike para búsquedas semánticas futuras
            print("📦 Saving extracted WhatsApp chats to semantic database (Spike)...")
            save_page_context(page.url, "WhatsApp Recent Chats Extraction", chats_text)
            return chats_text
        return "No text found in recent chats."
    except Exception as e:
        return f"Error extracting chats: {e}"

def _maybe_qr(result):
    """Si el resultado son bytes QR, devuelve texto + imagen. Si es str, devuélvelo tal cual."""
    global _qr_screenshot_bytes
    if isinstance(result, bytes) and result:
        from mcp.types import TextContent as _TextContent
        return [
            _TextContent(
                type="text",
                text="📱 WhatsApp requiere autenticación. Escanea este código QR con tu teléfono:\n"
                     "   1. Abre WhatsApp en tu teléfono\n"
                     "   2. Menú → WhatsApp Web → Escanear código\n"
                     "   3. Apunta la cámara a esta imagen\n\n"
                     "Después de escanear, vuelve a intentar la operación."
            ),
            MCPImage(data=result, format="png")
        ]
    return result

@mcp.tool()
async def read_whatsapp_messages(contact: str = None, limit: int = 15):
    """
    Lee los mensajes de WhatsApp usando el navegador actual.
    Si 'contact' es omitido, devuelve la lista de chats recientes de la bandeja principal.
    Si 'contact' es provisto (ej. 'Alexa'), busca semánticamente ese contacto,
    lo abre y extrae su historial de conversación reciente (hasta 'limit' mensajes).
    Para que funcione correctamente, asegúrate de cambiar el motor a 'chromium' usando set_browser_engine.
    
    Si WhatsApp pide código QR, devuelve la imagen del QR directamente.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error connecting to browser: {e}"

        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        
        context = contexts[0]
        if contact:
            result = await run_read_whatsapp_contact_chat(context, contact, limit)
        else:
            result = await run_read_whatsapp_chats(context, limit)
        return _maybe_qr(result)

@mcp.tool()
async def send_whatsapp_message(contact: str, message: str):
    """
    Send a message to a WhatsApp contact or group via web.whatsapp.com through Obscura.
    
    Si WhatsApp pide código QR, devuelve la imagen del QR directamente.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to browser on {cdp_url}. Details: {e}"

        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."

        context = contexts[0]
        result = await run_whatsapp(context, contact, message)
        return _maybe_qr(result)

@mcp.tool()
async def scroll_page(direction: str, amount: int = 500) -> str:
    """
    Scroll the active webpage or chat window in the browser.
    :param direction: 'up' to scroll towards the top, 'down' to scroll towards the bottom.
    :param amount: The number of pixels to scroll by (default: 500).
    """
    if direction not in ("up", "down"):
        return "Error: direction must be 'up' or 'down'."
        
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
            
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        pages = context.pages
        if not pages:
            return "Error: No pages open in the browser."
            
        # Get active or last active page
        page = pages[-1]
        for p_tab in pages:
            if p_tab.url != "about:blank":
                page = p_tab
                if "web.whatsapp.com" in p_tab.url:
                    page = p_tab
                    break
        
        try:
            result = await page.evaluate("""([dir, px]) => {
                const scrollPixels = dir === 'down' ? px : -px;
                
                // If it is WhatsApp Web, try to scroll the active chat pane
                if (window.location.hostname.includes('web.whatsapp.com')) {
                    const scrollable = document.querySelector('#main div[data-testid="conversation-panel-messages"]') || 
                                       document.querySelector('#main div[role="region"]') ||
                                       document.querySelector('#main .copyable-area > div:nth-child(2)') ||
                                       document.querySelector('#main .copyable-area').firstElementChild;
                    if (scrollable) {
                        scrollable.scrollBy(0, scrollPixels);
                        return `Scrolled WhatsApp chat ${dir} by ${px}px.`;
                    }
                }
                
                // General page scroll
                window.scrollBy(0, scrollPixels);
                return `Scrolled webpage ${dir} by ${px}px.`;
            }""", [direction, amount])
            
            return result
        except Exception as e:
            return f"Error executing scroll: {e}"

@mcp.tool()
async def sync_page_content() -> str:
    """
    Extract the content of the currently active webpage or chat, generate embeddings
    using Spike, and save it to the session database for semantic searching.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
            
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        pages = context.pages
        if not pages:
            return "Error: No pages open in the browser."
            
        # Prioritize non-blank pages, especially WhatsApp if open
        page = pages[-1]
        for p_tab in pages:
            if p_tab.url != "about:blank":
                page = p_tab
                if "web.whatsapp.com" in p_tab.url:
                    page = p_tab
                    break
                    
        try:
            url = page.url
            title = await page.title()
            
            # Extract content based on site type
            is_whatsapp = "web.whatsapp.com" in url
            
            if is_whatsapp:
                content = await page.evaluate("""() => {
                    const msgElements = document.querySelectorAll('#main .message-in, #main .message-out');
                    if (msgElements.length === 0) {
                        return "";
                    }
                    const msgs = [];
                    msgElements.forEach(el => {
                        const copyable = el.querySelector('.copyable-text');
                        let timestamp = '';
                        let sender = '';
                        let text = '';
                        
                        if (copyable) {
                            const preText = copyable.getAttribute('data-pre-plain-text');
                            if (preText) {
                                timestamp = preText.split(']')[0].replace('[', '').trim();
                                sender = preText.split(']')[1].replace(':', '').trim();
                            }
                            
                            const textSpan = copyable.querySelector('span.selectable-text');
                            text = textSpan ? textSpan.innerText.trim() : copyable.innerText.trim();
                        } else {
                            text = el.innerText.trim();
                        }
                        
                        const direction = el.classList.contains('message-in') ? 'incoming' : 'outgoing';
                        const senderName = sender ? sender : (direction === 'incoming' ? 'Incoming' : 'You');
                        const timeStr = timestamp ? ` [${timestamp}]` : '';
                        
                        if (text) {
                            msgs.push(`${senderName}${timeStr}: ${text}`);
                        }
                    });
                    return msgs.join('\\n');
                }""")
                if not content:
                    return "Error: No message content found on the WhatsApp page. Make sure a chat is open."
                title = f"WhatsApp Chat: {title}"
            else:
                # General webpage extraction
                content = await page.evaluate("""() => {
                    const toRemove = document.querySelectorAll('script, style, iframe, nav, footer, header, noscript');
                    toRemove.forEach(el => el.remove());
                    const mainContent = document.querySelector('article') || document.querySelector('main') || document.body;
                    return mainContent.innerText.replace(/\\n\\s*\\n+/g, '\\n\\n').trim();
                }""")
                
            if not content or not content.strip():
                return "Error: Page content is empty."
                
            # Save context (this automatically calls Spike and saves to the database)
            save_page_context(url, title, content)
            return f"✓ Page content from '{title}' successfully synced and indexed with Spike."
            
        except Exception as e:
            return f"Error syncing page content: {e}"

@mcp.tool()
async def get_browser_logs(duration_seconds: int = 5) -> str:
    """
    Passively listen to the console errors/warnings and failed network requests on the active browser tab
    for a given duration (default 5 seconds).
    Useful for diagnosing issues in frontend applications without triggering bot detection.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
            
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        pages = context.pages
        if not pages:
            return "Error: No pages open in the browser."
            
        # Prioritize active non-blank page
        page = pages[-1]
        for p_tab in pages:
            if p_tab.url != "about:blank":
                page = p_tab
                if "web.whatsapp.com" in p_tab.url:
                    page = p_tab
                    break

        console_messages = []
        network_failures = []
        
        # Define listeners
        def handle_console(msg):
            if msg.type in ("error", "warning"):
                console_messages.append(f"[{msg.type.upper()}] {msg.text}")
                
        def handle_request_failed(request):
            err_msg = request.failure if request.failure else "Unknown Error"
            network_failures.append(f"[FAILED] {request.method} {request.url} - {err_msg}")
            
        def handle_response(response):
            if response.status >= 400:
                network_failures.append(f"[HTTP {response.status}] {response.request.method} {response.url}")
                
        # Register listeners
        page.on("console", handle_console)
        page.on("requestfailed", handle_request_failed)
        page.on("response", handle_response)
        
        print(f"Listening to browser console/network events on '{await page.title()}' for {duration_seconds}s...")
        await asyncio.sleep(duration_seconds)
        
        # Unsubscribe
        page.remove_listener("console", handle_console)
        page.remove_listener("requestfailed", handle_request_failed)
        page.remove_listener("response", handle_response)
        
        report = []
        report.append(f"=== Browser Log Report (Duration: {duration_seconds}s) ===")
        report.append(f"Page: {page.url}")
        
        report.append("\n--- Console Errors / Warnings ---")
        if console_messages:
            report.extend(console_messages)
        else:
            report.append("(None)")
            
        report.append("\n--- Failed Network Requests (XHR/Fetch/Resources) ---")
        if network_failures:
            report.extend(network_failures)
        else:
            report.append("(None)")
            
        return "\n".join(report)

@mcp.tool()
async def wait_for_condition(selector: str, timeout_ms: int = 5000) -> str:
    """
    Wait for a specific element (specified by CSS selector, XPath, or data-testid) to appear on the active browser tab.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
            
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        pages = context.pages
        if not pages:
            return "Error: No pages open in the browser."
            
        page = pages[-1]
        for p_tab in pages:
            if p_tab.url != "about:blank":
                page = p_tab
                if "web.whatsapp.com" in p_tab.url:
                    page = p_tab
                    break
                    
        try:
            await page.wait_for_selector(selector, timeout=timeout_ms)
            return f"✓ Element matching selector '{selector}' appeared successfully within {timeout_ms}ms."
        except Exception as e:
            return f"Timeout/Error waiting for selector '{selector}': {e}"

@mcp.tool()
async def open_in_browser(url: str) -> str:
    """
    Open a URL in a new tab of the active browser window so the user can see it.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
            
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return f"✓ Successfully opened {url} in a new tab."
        except Exception as e:
            return f"Opened tab, but navigation encountered an error: {e}"

def check_already_commented(target_id: str, content_hash: str) -> bool:
    db_dir = _db_dir()
    db_path = os.path.join(db_dir, "simbad_browser.db")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS interactions_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT,
                target_id TEXT,
                content_hash TEXT,
                details TEXT,
                post_url TEXT,
                timestamp TEXT
            )
        """)
        # Alter table if column doesn't exist (backward compatibility)
        try:
            cursor.execute("ALTER TABLE interactions_history ADD COLUMN post_url TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        
        # 1. Check if content_hash already exists (duplicate post)
        cursor.execute("""
            SELECT id FROM interactions_history 
            WHERE platform = 'linkedin' AND content_hash = ?
        """, (content_hash,))
        if cursor.fetchone():
            conn.close()
            return True
            
        # 2. Check if we commented on this target_id (author) in the last 24 hours
        cursor.execute("""
            SELECT timestamp FROM interactions_history
            WHERE platform = 'linkedin' AND target_id = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (target_id,))
        row = cursor.fetchone()
        if row and row[0]:
            try:
                last_time = datetime.fromisoformat(row[0])
                if datetime.utcnow() - last_time < timedelta(hours=24):
                    print(f"Author {target_id} already received a comment in the last 24 hours. Skipping.")
                    conn.close()
                    return True
            except Exception as dt_e:
                print(f"Error parsing date {row[0]}: {dt_e}")
                
        conn.close()
        return False
    except Exception as e:
        print(f"Error checking interaction history: {e}")
        return False

def record_comment(target_id: str, content_hash: str, details: str, post_url: str = ""):
    db_dir = _db_dir()
    db_path = os.path.join(db_dir, "simbad_browser.db")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS interactions_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT,
                target_id TEXT,
                content_hash TEXT,
                details TEXT,
                post_url TEXT,
                timestamp TEXT
            )
        """)
        # Alter table if column doesn't exist (backward compatibility)
        try:
            cursor.execute("ALTER TABLE interactions_history ADD COLUMN post_url TEXT")
        except sqlite3.OperationalError:
            pass
        cursor.execute("""
            INSERT INTO interactions_history (platform, target_id, content_hash, details, post_url, timestamp)
            VALUES ('linkedin', ?, ?, ?, ?, ?)
        """, (target_id, content_hash, details, post_url, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        print(f"  ✓ Recorded interaction in database for '{target_id}' (hash {content_hash[:8]}).")
    except Exception as e:
        print(f"Error recording interaction: {e}")


@mcp.tool()
async def get_feed_posts(limit: int = 5) -> str:
    """
    Scans the LinkedIn feed and returns a list of posts (author, text) that have NOT been commented on today.
    Use this to read posts before generating specific, tailored comments.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            contexts = browser.contexts
            if not contexts: return "Error: No browser contexts."
            page = None
            for ctx in contexts:
                for pg in ctx.pages:
                    if "linkedin.com" in pg.url:
                        page = pg
                        break
                if page: break
            if not page: return "Error: No LinkedIn tab found."
            
            results = []
            seen_hashes = set()
            
            for _ in range(3):
                posts = await page.locator('main [role="listitem"]').all()
                for post in posts:
                    if len(results) >= limit: break
                    text = await post.inner_text()

                    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    if content_hash in seen_hashes: continue
                    seen_hashes.add(content_hash)
                    
                    author = await post.evaluate('''el => {
                        const txt = el.innerText.toLowerCase();
                        if (txt.includes("promocionado") || txt.includes("promoted")) return "Unknown";
                        const links = Array.from(el.querySelectorAll('a'));
                        const found = links.find(l => l.href.includes("/in/") && l.innerText.trim() !== "");
                        return found ? found.innerText.trim().split("\\n")[0] : "Unknown";
                    }''')
                    clean_author = author.split('•')[0].strip()
                    name_words = clean_author.split()
                    if len(name_words) >= 4 and name_words[0] == name_words[2] and name_words[1] == name_words[3]:
                        clean_author = f"{name_words[0]} {name_words[1]}"
                        
                    if clean_author == "Unknown": continue
                    
                    # Check DB
                    if not check_already_commented(clean_author, content_hash):
                        results.append({"author": clean_author, "text": text[:500] + "..." if len(text)>500 else text})
                        
                if len(results) >= limit: break
                await page.evaluate("window.scrollBy(0, 1200)")
                await asyncio.sleep(2)
                
            return json.dumps(results, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"Error: {e}"

@mcp.tool()
async def comment_on_post(author_name: str, comment_text: str) -> str:
    """
    Type and submit a comment on a LinkedIn post in the active browser tab.
    Locates the post matching the author's name, checks local database
    to avoid duplicate commenting, types the comment, and clicks submit.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"
            
        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        pages = context.pages
        if not pages:
            return "Error: No pages open in the browser."
            
        # Find LinkedIn page
        page = None
        for p_tab in pages:
            if "linkedin.com" in p_tab.url:
                page = p_tab
                break
        if not page:
            return "Error: No LinkedIn tab found in the browser."
            
        # Ensure we are on the feed page
        if "linkedin.com/feed" not in page.url:
            print(f"Current URL is {page.url}. Navigating to LinkedIn feed...")
            await page.goto("https://www.linkedin.com/feed/")
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)
            
        # 1. Find the post item matching the author, scrolling if necessary
        print(f"Scanning for post by author '{author_name}'...")
        target_index = None
        target_text = None
        target_clean_author = None
        attempt = 0
        
        while target_index is None and attempt < 8:
            attempt += 1
            posts_count = await page.locator('main [role="listitem"]').count()
            print(f"Scan attempt {attempt}: Found {posts_count} posts in DOM.")
            
            for i in range(posts_count):
                item = page.locator('main [role="listitem"]').nth(i)
                text = await item.inner_text()
                
                # Robust link-based author extraction
                author = await item.evaluate('''el => {
                    const txt = el.innerText.toLowerCase();
                    if (txt.includes("promocionado") || txt.includes("promoted")) return "Unknown";
                    const links = Array.from(el.querySelectorAll('a'));
                    const found = links.find(l => l.href.includes("/in/") && l.innerText.trim() !== "");
                    if (!found) return "Unknown";
                    return found.innerText.trim().split("\\n")[0];
                }''')
                
                clean_author = author.split('•')[0].strip()
                # Clean up repeated names in aria-labels or innerText (e.g. "Name Name")
                name_words = clean_author.split()
                if len(name_words) >= 4 and name_words[0] == name_words[2] and name_words[1] == name_words[3]:
                    clean_author = f"{name_words[0]} {name_words[1]}"
                
                if author_name.lower() in clean_author.lower():
                    target_index = i
                    target_text = text
                    target_clean_author = clean_author
                    print(f"  ✓ Found matching author '{clean_author}' at post index {i}.")
                    break
                    
            if target_index is None:
                # Check if "Cargar más" is visible and click it, otherwise scroll
                load_more_btn = page.locator('button:has-text("Cargar más"), button:has-text("Cargar nuevos posts"), button:has-text("Load more")')
                if await load_more_btn.count() > 0 and await load_more_btn.first.is_visible():
                    print("  - Clicking Cargar más button...")
                    await load_more_btn.first.click()
                    await asyncio.sleep(4)
                else:
                    print("  - Author not found in view. Scrolling workspace down...")
                    await page.evaluate("""() => {
                        const el = document.getElementById('workspace') || document.querySelector('main');
                        if (el) el.scrollBy(0, 1200);
                    }""")
                    await asyncio.sleep(3)
                    
        if target_index is None:
            return f"Error: Post by author '{author_name}' was not found in the feed after scrolling."
            
        # 2. Database guard check to prevent duplication
        content_hash = hashlib.sha256(target_text.encode("utf-8")).hexdigest()
        
        if check_already_commented(target_clean_author, content_hash):
            return f"Warning: Post by author '{target_clean_author}' (hash '{content_hash[:8]}') has already been commented on. Skipping to prevent duplicate."
            
        # 3. Interact with target post
        post_locator = page.locator('main [role="listitem"]').nth(target_index)
        
        # Center the post container to prevent sticky header occlusion and ensure visibility
        print("Centering post in viewport...")
        await post_locator.evaluate("el => el.scrollIntoView({ behavior: 'instant', block: 'center' })")
        await asyncio.sleep(2)
        
        # Extract post URL link
        post_url = await post_locator.evaluate('''el => {
            const links = Array.from(el.querySelectorAll('a'));
            const found = links.find(l => 
                (l.href.includes("urn:li:") && (l.href.includes("/update/") || l.href.includes("/share/") || l.href.includes("/activity/") || l.href.includes("/ugcPost/"))) ||
                l.href.includes("/posts/") || l.href.includes("/pulse/")
            );
            return found ? found.href : "";
        }''')
        print(f"  - Extracted post URL: {post_url}")
        
        comment_box = post_locator.locator('div[role="textbox"]')
        
        if await comment_box.count() == 0:
            print("Opening comment editor...")
            comment_btn = post_locator.locator('button[aria-label="Comentar"], button[aria-label^="Comment"]')
            if await comment_btn.count() > 0:
                await comment_btn.first.click()
                await asyncio.sleep(2)
        
        # Check the DOM to see if the active user already commented
        already_commented_in_dom = await post_locator.evaluate('''el => {
            const myImg = document.querySelector('.global-nav__me-photo');
            if (myImg && myImg.alt) {
                const myName = myImg.alt.toLowerCase();
                const commenters = Array.from(el.querySelectorAll('.comments-post-meta__name-text, span.comments-post-meta__name, .comments-comment-meta__description-title'));
                const found = commenters.find(c => c.innerText.toLowerCase().includes(myName));
                if (found) return true;
            }
            // Fallback check
            const deleteBtns = el.querySelectorAll('button[aria-label*="Eliminar comentario"], button[aria-label*="Delete comment"]');
            return deleteBtns.length > 0;
        }''')
        
        if already_commented_in_dom:
            print("  ✓ Found existing comment by active user in DOM. Aborting to prevent duplicate.")
            return f"Warning: You already have a comment on this post in the DOM. Aborted to prevent duplicate."
            
        # 4. Focus, clear, and type comment
        print("Typing comment...")
        await comment_box.focus()
        await comment_box.fill("")
        await page.keyboard.type(comment_text, delay=35)
        await asyncio.sleep(1.5)
        
        # 5. Click Comentar/Publicar button using JS evaluator to bypass selector/obfuscation changes
        print("Submitting comment...")
        submit_success = await post_locator.evaluate('''el => {
            const buttons = Array.from(el.querySelectorAll('button'));
            const found = buttons.find(b => {
                const txt = b.innerText.trim().toLowerCase();
                return txt === 'comentar' || txt === 'comment' || txt === 'publicar' || txt === 'post';
            });
            if (found) {
                found.scrollIntoView({ behavior: 'instant', block: 'center' });
                found.click();
                return true;
            }
            return false;
        }''')
        
        if submit_success:
            print("  - Clicked submit button. Verifying publication...")
            
            # Post-click verification: Wait until the comment box is cleared or collapses
            is_successful = False
            for check in range(12):
                try:
                    box_count = await comment_box.count()
                    if box_count == 0:
                        is_successful = True
                        break
                    box_text = await comment_box.inner_text()
                    if box_text.strip() == "":
                        is_successful = True
                        break
                except Exception:
                    # Detached element means it collapsed/succeeded!
                    is_successful = True
                    break
                await asyncio.sleep(0.5)
                
            if not is_successful:
                print("  - Comment box was not cleared. Retrying submit click...")
                try:
                    await post_locator.evaluate('''el => {
                        const buttons = Array.from(el.querySelectorAll('button'));
                        const found = buttons.find(b => {
                            const txt = b.innerText.trim().toLowerCase();
                            return txt === 'comentar' || txt === 'comment' || txt === 'publicar' || txt === 'post';
                        });
                        if (found) found.click();
                    }''')
                except Exception as e:
                    print(f"  - Retry click threw exception (might have succeeded just now): {e}")
                await asyncio.sleep(3)
                
                # Check again
                box_count = await comment_box.count()
                if box_count > 0:
                    box_text = await comment_box.inner_text()
                    if box_text.strip() != "":
                        return "Error: Comment box did not clear after multiple submission attempts."
            
            # Record success in the database
            record_comment(target_clean_author, content_hash, comment_text, post_url)
            return f"✓ Comment successfully posted on '{author_name}''s post."
        else:
            return "Error: Could not locate the submit button for the comment box."

async def shadow_eval(page, expression):
    """Evaluate JavaScript inside LinkedIn's shadow DOM (#interop-outlet)"""
    return await page.evaluate(f"""() => {{
        const root = document.querySelector('#interop-outlet')?.shadowRoot;
        if (!root) return 'NO_SHADOW';
        return ({expression});
    }}""")

async def shadow_get_pos(page, selector):
    """Get center coordinates of a shadow DOM element for mouse click"""
    result = await page.evaluate(f"""() => {{
        const root = document.querySelector('#interop-outlet')?.shadowRoot;
        if (!root) return null;
        const el = root.querySelector('{selector}');
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        return {{x: rect.left + rect.width/2, y: rect.top + rect.height/2}};
    }}""")
    return result

async def shadow_get_pos_js(page, js_selector):
    """Get center coordinates using a JS expression that returns an element"""
    result = await page.evaluate(f"""() => {{
        const root = document.querySelector('#interop-outlet')?.shadowRoot;
        if (!root) return null;
        const el = ({js_selector});
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        return {{x: Math.round(rect.left + rect.width/2), y: Math.round(rect.top + rect.height/2)}};
    }}""")
    return result

async def shadow_click(page, selector_or_js, use_js=False):
    """Click an element inside shadow DOM using real mouse events (bypasses React overlay)"""
    if use_js:
        pos = await shadow_get_pos_js(page, selector_or_js)
    else:
        pos = await shadow_get_pos(page, selector_or_js)
    if not pos:
        return False
    await page.mouse.click(pos["x"], pos["y"])
    return True

@mcp.tool()
async def publish_linkedin_post(content: str) -> str:
    """
    Create and publish a new post on LinkedIn in the active browser tab.
    Opens the post creation dialog, types the content, and submits it.
    Works with LinkedIn's Shadow DOM and settings confirmation screen.
    """
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"

        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        pages = context.pages
        if not pages:
            return "Error: No pages open in the browser."

        # Find LinkedIn page
        page = None
        for p_tab in pages:
            if "linkedin.com" in p_tab.url:
                page = p_tab
                break
        if not page:
            return "Error: No LinkedIn tab found in the browser."

        # Navigate to clean feed URL (without query params that trigger overlays)
        clean_feed = "https://www.linkedin.com/feed/"
        if page.url != clean_feed:
            print(f"Current: {page.url} → navigating to clean feed...")
            await page.goto(clean_feed)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)

        # ---- CHECK CURRENT STATE ----
        current_state = await shadow_eval(page, """() => {
            const d = root.querySelector('[role="dialog"]');
            if (!d) return 'NO_DIALOG';
            if (root.querySelector('[role="textbox"], [contenteditable="true"]')) return 'EDITOR';
            return 'SETTINGS';
        }""")

        # ---- STEP 1: OPEN DIALOG IF NEEDED ----
        if current_state == 'NO_DIALOG':
            print("Step 1: Opening create post dialog...")
            # Try inside shadow DOM first
            clicked = await shadow_click(page, '[aria-label="Crear publicación"]')
            if not clicked:
                # Fallback: search in main page DOM
                print("  Trying main page DOM...")
                for selector in [
                    '[aria-label="Crear publicación"]',
                    '[aria-label="Start a post"]',
                    '.share-box-feed-entry__trigger',
                    '[data-control-name="create_post"]'
                ]:
                    pos = await page.evaluate(f"""() => {{
                        const el = document.querySelector('{selector}');
                        if (!el) return null;
                        const rect = el.getBoundingClientRect();
                        return {{x: Math.round(rect.left + rect.width/2), y: Math.round(rect.top + rect.height/2)}};
                    }}""")
                    if pos:
                        await page.mouse.click(pos["x"], pos["y"])
                        clicked = True
                        break
            if not clicked:
                return "Error: Could not find 'Crear publicación' button in LinkedIn feed."
            print("  ✓ Create button clicked")
            await asyncio.sleep(3)
            current_state = await shadow_eval(page, """() => {
                const d = root.querySelector('[role="dialog"]');
                if (!d) return 'NO_DIALOG';
                if (root.querySelector('[role="textbox"], [contenteditable="true"]')) return 'EDITOR';
                return 'SETTINGS';
            }""")

        # ---- STEP 2: SETTINGS SCREEN (CONFIRM VISIBILITY) ----
        if current_state == 'SETTINGS':
            print("Step 2: Confirming publication settings...")

            # LinkedIn disables "Hecho" until a change is detected.
            # Toggle CONNECTIONS_ONLY -> ANYONE to trigger enable.
            for toggle_id in ['CONNECTIONS_ONLY', 'ANYONE']:
                await shadow_click(page, f'#{toggle_id}', use_js=True)
                await asyncio.sleep(0.5)

            await asyncio.sleep(1)

            # Click "Hecho" (should be enabled now)
            hecho_clicked = await shadow_click(page, '.share-box-footer__primary-btn')
            if not hecho_clicked:
                return "Error: Could not find 'Hecho' button in settings dialog."
            print("  ✓ Settings confirmed (Hecho clicked)")
            await asyncio.sleep(4)

        # ---- STEP 3: FIND EDITOR AND TYPE CONTENT ----
        print("Step 3: Finding editor...")
        editor_found = False
        for attempt in range(10):
            editor_check = await shadow_eval(page, """() => {
                const ed = root.querySelector('[role="textbox"], [contenteditable="true"]');
                if (ed) { ed.focus(); return 'OK'; }
                return 'WAIT';
            }""")
            if editor_check == 'OK':
                editor_found = True
                break
            await asyncio.sleep(1)

        if not editor_found:
            return "Error: Could not find post editor after 10 seconds."

        print("  ✓ Editor found, typing content...")
        # Use Playwright's Shadow DOM piercing to get the editor element
        # then type naturally with human_type (character by character with delays)
        editor = page.locator('#interop-outlet').locator('[contenteditable="true"]')
        await editor.first.click()
        await asyncio.sleep(0.5)
        # Clear existing content via JS
        await editor.first.evaluate('el => el.innerHTML = ""')
        await asyncio.sleep(0.3)
        # Type naturally — human_type handles \n as Enter key
        await human_type(editor.first, content)
        print(f"  ✓ Content typed ({len(content)} chars)")
        await asyncio.sleep(2)

        # ---- STEP 4: PUBLISH ----
        print("Step 4: Publishing...")

        # Find "Publicar"/"Post" button real position
        pub_pos = await shadow_get_pos_js(page, """() => {
            const buttons = Array.from(root.querySelectorAll('button'));
            return buttons.find(b => {
                const t = b.innerText.trim().toLowerCase();
                return (t === 'publicar' || t === 'post') && !b.disabled;
            });
        }""")

        if not pub_pos:
            # Debug: list all available buttons
            buttons_info = await shadow_eval(page, """() => {
                return Array.from(root.querySelectorAll('button'))
                    .filter(b => b.offsetParent !== null)
                    .map(b => b.innerText.trim() + (b.disabled ? '(disabled)' : ''))
                    .filter(t => t).join(' | ');
            }""")
            return f"Error: Could not find 'Publicar' button. Available: {buttons_info}"

        await page.mouse.click(pub_pos["x"], pub_pos["y"])
        print("  ✓ Publish button clicked")
        await asyncio.sleep(5)

        # ---- VERIFY ----
        modal_closed = await shadow_eval(page, """() => {
            return root.querySelector('[role="dialog"]') ? 'OPEN' : 'CLOSED';
        }""")

        if modal_closed == 'CLOSED':
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            record_comment("self_publish", content_hash, content, "https://www.linkedin.com/feed/")
            print("  ✓ Post published successfully!")
            return "✓ Post successfully published on LinkedIn!"
        else:
            return "Warning: Modal did not close after publishing. The post may or may not have been published."




async def run_read_whatsapp_contact_chat(context, contact: str, limit: int = 15):
    pages = context.pages
    page = None
    for p in pages:
        if "web.whatsapp.com" in p.url:
            page = p
            break

    if not page:
        page = await context.new_page()
        await page.goto("https://web.whatsapp.com")

    # Wait for chat list
    try:
        await page.wait_for_selector('#pane-side, div[aria-label="Lista de chats"]', timeout=15000)
    except Exception:
        qr_bytes = await _check_qr_and_capture(page)
        if qr_bytes:
            return qr_bytes
        return "Error: Could not find chat list. WhatsApp might still be loading."

    try:
        # ── 1. SEARCH CONTACT ──
        # Use a universal selector that works on both WhatsApp Web and Business
        search_box = page.locator('[role="textbox"]').first
        await search_box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.3)
        await search_box.fill(contact)
        await asyncio.sleep(2)

        # ── 2. CLICK FIRST RESULT ──
        clicked = await page.evaluate("""(contactName) => {
            const rows = document.querySelectorAll('#pane-side div[role="row"]');
            // Try exact match first, then partial
            for (const row of rows) {
                const text = row.innerText.toLowerCase();
                if (text.includes(contactName.toLowerCase())) {
                    const rect = row.getBoundingClientRect();
                    row.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true,
                        clientX: rect.left + 10, clientY: rect.top + 10}));
                    return true;
                }
            }
            // Fallback: click first row
            if (rows.length > 0) {
                rows[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                return 'fallback';
            }
            return false;
        }""", contact)
        if not clicked:
            await search_box.press("Enter")
        await asyncio.sleep(2)

        # ── 3. VERIFY WITH SPIKE ──
        # Find the open chat title without relying on #main
        actual_chat_title = await page.evaluate("""() => {
            const spans = document.querySelectorAll('span[dir="auto"]');
            const exclude = ['Chats', 'Buscar un chat o iniciar uno nuevo', 'Search or start new chat',
                             'Actualizaciones en Estados', 'Status updates', '', 'Lista de chats', 'Chat list'];
            for (const s of spans) {
                const t = s.innerText.trim();
                if (t && !exclude.includes(t) && t.length > 1) return t;
            }
            return 'Unknown';
        }""")

        if actual_chat_title != 'Unknown':
            emb1 = get_ollama_embedding(f"search_query: {contact.lower()}")
            emb2 = get_ollama_embedding(f"search_query: {actual_chat_title.lower()}")
            if emb1 and emb2:
                similarity = cosine_similarity(emb1, emb2)
                if similarity < 0.75:
                    return (f"⚠️ Security abort: Chat opened ('{actual_chat_title}') does not match "
                            f"requested contact ('{contact}'). Spike similarity: {similarity:.2f}")

        # ── 4. EXTRACT MESSAGES ──
        await asyncio.sleep(2)
        messages_data = await page.evaluate("""(limit) => {
            // Find the container with most [data-testid*="msg"] children
            const allDivs = document.querySelectorAll('div');
            let bestContainer = null, bestCount = 0;
            allDivs.forEach(d => {
                const count = d.querySelectorAll('[data-testid*="msg"]').length;
                if (count > bestCount) { bestCount = count; bestContainer = d; }
            });
            if (!bestContainer || bestCount === 0) return [];

            const msgEls = bestContainer.querySelectorAll('[data-testid*="msg"]');
            const results = [];
            msgEls.forEach(el => {
                const html = el.outerHTML;
                const text = el.innerText.trim();
                if (!text || text.length < 2) return;

                // Determine direction: outgoing messages usually have different data-testid or CSS
                const isOutgoing = html.includes('message-out') || html.includes('msg-outgoing')
                    || el.closest('[data-testid*="outgoing"]') !== null
                    || (el.getAttribute('data-testid') || '').includes('outgoing');

                results.push({
                    text: text,
                    isOutgoing: isOutgoing
                });
            });
            return results.slice(-limit);
        }""", limit)

        if not messages_data:
            html_preview = await page.evaluate("document.body.innerText.slice(0, 500)")
            return f"No messages found in chat with {actual_chat_title}. Page preview: {html_preview}"

        # Format the output
        result_lines = [f"--- Chat history with {actual_chat_title} ---"]
        for msg in messages_data:
            sender = "You" if msg['isOutgoing'] else actual_chat_title
            # Clean up the text
            clean_text = " | ".join(
                line.strip() for line in msg['text'].split('\n') if line.strip()
            )
            result_lines.append(f"{sender}: {clean_text}")

        chat_text = "\n".join(result_lines)
        # Save with Spike for future semantic queries
        save_page_context(page.url, f"WhatsApp Chat History with {actual_chat_title}", chat_text)
        return chat_text

    except Exception as e:
        return f"Error reading chat: {e}"

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Health check endpoint for the orchestrator."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "engine": CURRENT_ENGINE})

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Simbad MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                       help="Transport protocol (default: stdio)")
    parser.add_argument("--host", default="127.0.0.1",
                       help="Host to bind SSE server (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9861,
                       help="Port for SSE server (default: 9861)")
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        sys.stderr.write(f"🌐 Simbad MCP SSE server on http://{args.host}:{args.port}/sse\n")
        sys.stderr.flush()
        mcp.run(transport="sse")
    else:
        sys.stderr.write("✅ Simbad MCP Server listo. Esperando conexiones de OpenCode/Claude...\n")
        sys.stderr.flush()
        mcp.run()
