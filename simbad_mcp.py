import asyncio
import os
import re
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

# Sitios que requieren sesión iniciada → fuerzan el motor Chromium (9227).
# Incluye redes sociales y portales inmobiliarios con login.
LOGIN_SITES = ["linkedin.com", "web.whatsapp.com", "facebook.com", "fb.com",
               "buscocasita", "adondevivir", "infocasas", "urbania",
               "casafiel", "lamudi", "nexoinmobiliario", "doomos"]

# JS compartido para extraer el nombre del autor de un post del feed de LinkedIn.
# Prioriza el link del creador, excluye posts promocionados/publicidad y limpia prefijos de la interfaz.
_AUTHOR_EXTRACT_JS = r'''el => {
    const txt = el.innerText.toLowerCase();
    if (txt.includes("promocionado") || txt.includes("promoted") || txt.includes("publicidad")) return "Unknown";

    const creator = el.querySelector(".update-components-actor__title a, .update-components-actor__name a, .update-components-actor a, a[href*='/in/'], a[href*='/company/']");
    if (creator && creator.innerText.trim() !== "") {
        let name = creator.innerText.trim().split(String.fromCharCode(10))[0].trim();
        name = name.replace(/^Publicación en el feed\s*/i, '').replace(/^Sugerencias\s*/i, '').trim();
        if (name) return name;
    }

    const header = el.querySelector(".update-components-header, .update-components-text-view");
    const headerLinks = header ? Array.from(header.querySelectorAll("a")) : [];
    const links = Array.from(el.querySelectorAll("a"));
    const found = links.find(l =>
        (l.href.includes("/in/") || l.href.includes("/company/")) &&
        l.innerText.trim() !== "" &&
        !headerLinks.includes(l)
    );
    if (found) {
        let name = found.innerText.trim().split(String.fromCharCode(10))[0].trim();
        name = name.replace(/^Publicación en el feed\s*/i, '').replace(/^Sugerencias\s*/i, '').trim();
        if (name) return name;
    }
    return "Unknown";
}'''

# JS compartido para extraer el permalink (URL) de un post del feed de LinkedIn.
# LinkedIn rediseñó su DOM (clases ofuscadas) y ya NO expone la URN del post en
# el HTML estático. El método fiable es: menú de controles → "Copiar enlace a la
# publicación", que escribe la URL real vía navigator.clipboard.writeText().
# Mantenemos los métodos clásicos como fallback por si el DOM vuelve a cambiar.
_POST_URL_EXTRACT_JS = r'''async el => {
    const hrefs = Array.from(el.querySelectorAll('a')).map(a => a.href);
    // 1) Permalink específico del post (si LinkedIn vuelve a exponerlo en el DOM)
    let found = hrefs.find(h =>
        h.includes("/feed/update/urn:li:") ||
        h.includes("urn:li:activity:") ||
        h.includes("urn:li:ugcPost:") ||
        h.includes("/update/urn:li:") ||
        (h.includes("/posts/") && !h.endsWith("/posts/") && h.includes("activity"))
    );
    if (found) return found;

    // 2) URN en atributos o en el HTML del post
    const allElems = [el, ...Array.from(el.querySelectorAll('*'))];
    for (const elem of allElems) {
        for (const attr of elem.attributes) {
            const val = attr.value || '';
            if (val.includes('urn:li:activity:') || val.includes('urn:li:ugcPost:') || val.includes('urn:li:share:')) {
                const match = val.match(/urn:li:(activity|ugcPost|share):(\d+)/);
                if (match) {
                    return `https://www.linkedin.com/feed/update/urn:li:${match[1]}:${match[2]}/`;
                }
            }
        }
    }
    const htmlMatch = el.outerHTML.match(/urn:li:(activity|ugcPost|share):(\d+)/i);
    if (htmlMatch) {
        return `https://www.linkedin.com/feed/update/urn:li:${htmlMatch[1]}:${htmlMatch[2]}/`;
    }

    // 3) Método del portapapeles (DOM nuevo, nov 2025+):
    //    abrir menú de controles, interceptar clipboard.writeText, click "Copiar enlace".
    const menuBtn = el.querySelector('button[aria-label*="Abrir el menú de controles"], button[aria-label*="Open control menu"]');
    if (menuBtn) {
        // Cerrar cualquier menú previo con Escape (body.click ya NO cierra el menú en el DOM nuevo)
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
        await new Promise(r => setTimeout(r, 300));

        // Traer el botón al viewport para que el click abra el menú del post correcto
        menuBtn.scrollIntoView({ behavior: 'instant', block: 'center' });
        await new Promise(r => setTimeout(r, 200));

        // Interceptar clipboard
        const origWrite = navigator.clipboard && navigator.clipboard.writeText;
        let captured = null;
        if (origWrite) {
            navigator.clipboard.writeText = (text) => { captured = text; return origWrite.call(navigator.clipboard, text); };
        }

        menuBtn.click();
        await new Promise(r => setTimeout(r, 400));

        // Espera condicional: esperar a que aparezca "Copiar enlace a la publicación"
        let copyItem = null;
        for (let i = 0; i < 8; i++) {
            const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
            copyItem = items.find(mi => /copiar enlace/i.test(mi.innerText));
            if (copyItem) break;
            await new Promise(r => setTimeout(r, 250));
        }

        if (copyItem) {
            copyItem.click();
            for (let i = 0; i < 6; i++) {
                if (captured) break;
                await new Promise(r => setTimeout(r, 200));
            }
        }

        // Restaurar clipboard
        if (origWrite) navigator.clipboard.writeText = origWrite;

        // Cerrar menú (Escape)
        document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

        if (captured && captured.includes("linkedin.com")) return captured;
    }

    // 4) Fallback: link del timestamp o publicaciones de empresa
    const timeLink = el.querySelector(".update-components-actor__sub-headline a, .app-shared-update-v2__sub-headline-link, a[href*='/posts/']");
    if (timeLink) return timeLink.href;

    found = hrefs.find(h => h.includes("/posts/"));
    return found || "";
}'''

# Locator compartido para los posts del feed de LinkedIn.
# El DOM actual (nov 2025+) renderiza cada post como <li role="listitem">
# dentro de <main>. Los selectores clásicos (.feed-shared-update-v2, article,
# div[data-urn]) se mantienen como fallback por si el DOM vuelve a cambiar.
_POSTS_LOCATOR = 'main [role="listitem"], .feed-shared-update-v2, article, div[data-urn]'

# JS compartido para la Retina Semántica: extrae todos los elementos interactivos
# visibles del DOM, les asigna un ID numérico y calcula su bounding box.
_INTERACTIVE_MAP_JS = '''() => {
    const SELECTORS = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="menuitem"], [role="tab"], [role="switch"], [contenteditable="true"]';
    const els = Array.from(document.querySelectorAll(SELECTORS));
    const results = [];
    for (const el of els) {
        if (results.length >= 100) break;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        if (rect.bottom < 0 || rect.top > window.innerHeight) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || '';
        let name = el.getAttribute('aria-label')
            || el.getAttribute('placeholder')
            || el.getAttribute('title')
            || (tag === 'a' || tag === 'button' ? el.innerText : '')
            || el.getAttribute('alt')
            || '';
        name = name.trim().split('\n')[0].substring(0, 60);
        const entry = {
            tag: tag,
            role: role,
            name: name,
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            w: Math.round(rect.width),
            h: Math.round(rect.height)
        };
        if (tag === 'input' || tag === 'textarea' || tag === 'select') {
            entry.type = el.type || '';
            entry.value = (el.value || '').substring(0, 40);
        }
        if (tag === 'input' && el.type === 'checkbox') {
            entry.checked = el.checked;
        }
        if (tag === 'a' && el.href) {
            entry.href = el.href.substring(0, 100);
        }
        // Build a CSS selector for fallback click
        if (el.id) {
            entry.selector = '#' + CSS.escape(el.id);
        } else {
            const idx = Array.from(el.parentNode.children).filter(c => c.tagName === el.tagName).indexOf(el);
            const parent = el.parentNode;
            let parentSel = '';
            if (parent && parent !== document.body && parent !== document.documentElement) {
                if (parent.id) parentSel = '#' + CSS.escape(parent.id) + ' > ';
                else if (parent.className && typeof parent.className === 'string') {
                    const cls = parent.className.trim().split(/\\s+/)[0];
                    if (cls) parentSel = '.' + CSS.escape(cls) + ' > ';
                }
            }
            entry.selector = parentSel + tag + (idx > 0 ? ':nth-of-type(' + (idx + 1) + ')' : '');
        }
        results.push(entry);
    }
    results.sort((a, b) => a.y === b.y ? a.x - b.x : a.y - b.y);
    return results;
}'''


def _clean_author(author: str) -> str:
    """Normaliza el nombre de autor: quita ' • …' y colapsa nombres repetidos ('N A N A' → 'N A')."""
    clean = author.split('•')[0].strip()
    words = clean.split()
    if len(words) >= 4 and words[0] == words[2] and words[1] == words[3]:
        clean = f"{words[0]} {words[1]}"
    return clean


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
                       "History", "History-journal",
                       "Favicons", "Favicons-journal", "Top Sites",
                       "Shortcuts", "Shortcuts-journal", "Storage",
                       "Sync Data", "README", "Network Action Predictor",
                       "Network Persistent State", "TransportSecurity",
                       "Trusted Vault", "FileTypePolicies"}
    
    # Eliminar archivos y carpetas de sesión previa para evitar restaurar pestañas
    for f in ["Current Session", "Current Tabs", "Last Session", "Last Tabs"]:
        p = os.path.join(default_dir, f)
        if os.path.isfile(p):
            try:
                os.unlink(p)
            except Exception:
                pass
                
    sessions_dir = os.path.join(default_dir, "Sessions")
    if os.path.isdir(sessions_dir):
        try:
            shutil.rmtree(sessions_dir, ignore_errors=True)
        except Exception:
            pass

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

def save_page_context(url, title, content, source=None):
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

    # Deduce source if not explicitly provided
    if not source:
        if "whatsapp.com" in url:
            source = "whatsapp"
        elif "linkedin.com" in url:
            source = "linkedin"
        elif "chatgpt.com" in url or "chat.openai.com" in url:
            source = "chatgpt"
        elif is_project_cli:
            source = "cli"
        else:
            source = "web"
    
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
                timestamp TEXT,
                source TEXT
            )
        """)
        
        # Alter table if columns don't exist (backward compatibility)
        for col in ["embedding", "content_hash", "source"]:
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
            INSERT INTO {table_name} (url, title, content, embedding, content_hash, timestamp, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (url, title, content, embedding_json, content_hash, datetime.now().isoformat(), source))
        conn.commit()
        conn.close()
        try:
            os.chmod(db_path, 0o666)
        except Exception:
            pass
        print(f"Context saved to {table_name}: '{title}' ({url}) [source: {source}]")
    except Exception as e:
        print(f"Warning: Could not save context to {table_name}: {e}")


mcp = FastMCP("Simbad")

# Patch: make the server stateless so OpenCode can call tools immediately
# without requiring explicit initialize request after reconnection
_orig_run = mcp._mcp_server.run
async def _patched_run(read_stream, write_stream, initialization_options,
                       raise_exceptions=False, stateless=False):
    await _orig_run(read_stream, write_stream, initialization_options,
                    raise_exceptions=raise_exceptions, stateless=True)
mcp._mcp_server.run = _patched_run

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
      - 'obscura' (puerto 9222): Motor ultra-rápido, modo stealth. Ideal para scraping, búsqueda web, ChatGPT. Sin sesión persistente.
      - 'chromium' (puerto 9227): Chromium headless real. Para TODO lo que requiera login: LinkedIn, Facebook, WhatsApp, portales inmobiliarios, etc. Sesión persistente.
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

async def get_single_whatsapp_page(context):
    pages = context.pages
    whatsapp_pages = [p for p in pages if "web.whatsapp.com" in p.url]
    if whatsapp_pages:
        page = whatsapp_pages[0]
        await page.bring_to_front()
        # Cerrar cualquier pestaña duplicada de WhatsApp de forma segura
        for dup in whatsapp_pages[1:]:
            try:
                await dup.close()
            except Exception:
                pass
        return page
    else:
        page = await context.new_page()
        await page.goto("https://web.whatsapp.com")
        return page

async def get_single_linkedin_page(context, url=None):
    pages = context.pages
    linkedin_pages = [p for p in pages if "linkedin.com" in p.url]
    if linkedin_pages:
        page = linkedin_pages[0]
        await page.bring_to_front()
        # Cerrar cualquier pestaña duplicada de LinkedIn de forma segura
        for dup in linkedin_pages[1:]:
            try:
                await dup.close()
            except Exception:
                pass
        if url and page.url != url:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return page
    else:
        page = await context.new_page()
        target_url = url if url else "https://www.linkedin.com/feed/"
        await page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
        return page

async def run_whatsapp(context, contact, message):
    js_click = """el => {
        const r = el.getBoundingClientRect();
        const opts = { bubbles: true, cancelable: true, view: window, clientX: r.left + r.width/2, clientY: r.top + r.height/2 };
        el.dispatchEvent(new MouseEvent('mousedown', opts));
        el.dispatchEvent(new MouseEvent('mouseup', opts));
        el.dispatchEvent(new MouseEvent('click', opts));
    }"""

    page = await get_single_whatsapp_page(context)
        
    # First check if it's asking for a QR code
    qr_bytes = await _check_qr_and_capture(page)
    if qr_bytes:
        return qr_bytes

    try:
        # Press Escape first in case a chat is already open
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        # Search for the contact
        search_box = page.locator('[role="textbox"]').first
        await search_box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.3)
        await search_box.fill(contact)
        await asyncio.sleep(2)
        
        # Click the first matching contact in the sidebar
        clicked = await page.evaluate("""(contactName) => {
            const rows = document.querySelectorAll('#pane-side div[role="row"]');
            for (const row of rows) {
                const text = row.innerText.toLowerCase();
                if (text.includes(contactName.toLowerCase())) {
                    const cell = row.querySelector('[role="gridcell"]');
                    if (cell) {
                        cell.click();
                        return true;
                    }
                    row.click();
                    return true;
                }
            }
            if (rows.length > 0) {
                const cell = rows[0].querySelector('[role="gridcell"]');
                if (cell) cell.click();
                else rows[0].click();
                return 'fallback';
            }
            return false;
        }""", contact)
        if not clicked:
            await search_box.press("Enter")
        
        await asyncio.sleep(1)
        # Press Enter to open the conversation (needed for WhatsApp Business)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2)
            
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
        
        # Check if the source column exists dynamically
        cursor.execute(f"PRAGMA table_info({table_name})")
        cols = [c[1] for c in cursor.fetchall()]
        has_source = "source" in cols
        
        if has_source:
            cursor.execute(f"SELECT id, url, title, content, embedding, timestamp, source FROM {table_name}")
        else:
            cursor.execute(f"SELECT id, url, title, content, embedding, timestamp FROM {table_name}")
            
        rows = cursor.fetchall()
        conn.close()
        
        matches = []
        for row in rows:
            if has_source:
                row_id, url, title, content, embedding_json, timestamp, source = row
            else:
                row_id, url, title, content, embedding_json, timestamp = row
                source = None
                
            if not embedding_json:
                continue
                
            # Deduce source if not set
            if not source:
                if "whatsapp.com" in url:
                    source = "whatsapp"
                elif "linkedin.com" in url:
                    source = "linkedin"
                elif "chatgpt.com" in url or "chat.openai.com" in url:
                    source = "chatgpt"
                elif url.startswith("claude-code://") or url.startswith("antigravity://"):
                    source = "cli"
                else:
                    source = "web"
                    
            embedding = json.loads(embedding_json)
            score = cosine_similarity(query_vector, embedding)
            matches.append({
                "id": row_id,
                "url": url,
                "title": title,
                "content": content,
                "score": score,
                "timestamp": timestamp,
                "source": source
            })
            
        # Sort by similarity score descending
        matches.sort(key=lambda x: x["score"], reverse=True)
        
        formatted_matches = []
        for i, match in enumerate(matches[:5]): # Top 5 semantic matches
            source_tag = f" [{match['source'].upper()}]" if match['source'] else ""
            formatted_matches.append(
                f"[{i+1}]{source_tag} {match['title']} (Score: {match['score']:.4f})\n"
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
    global CURRENT_ENGINE
    if any(s in url.lower() for s in LOGIN_SITES):
        CURRENT_ENGINE = "chromium"
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
    page = await get_single_whatsapp_page(context)

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
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
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
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
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
            
        # Prioritize currently visible/active page, fallback to last page
        page = pages[-1]
        for p_tab in pages:
            try:
                is_visible = await p_tab.evaluate("document.visibilityState === 'visible'")
                if is_visible:
                    page = p_tab
                    break
            except Exception:
                pass
        
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
            
        # Prioritize currently visible/active page, fallback to last page
        page = pages[-1]
        for p_tab in pages:
            try:
                is_visible = await p_tab.evaluate("document.visibilityState === 'visible'")
                if is_visible:
                    page = p_tab
                    break
            except Exception:
                pass
                    
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
    If a tab with the same domain or URL is already open, it is focused and reused.
    """
    global CURRENT_ENGINE
    if any(s in url.lower() for s in LOGIN_SITES):
        CURRENT_ENGINE = "chromium"
        
    from urllib.parse import urlparse
    parsed_target = urlparse(url)
    target_host = parsed_target.netloc.lower()
    
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
        
        # Enforce single tab rules for WhatsApp and LinkedIn
        if "web.whatsapp.com" in url:
            try:
                await get_single_whatsapp_page(context)
                return f"✓ Reused and focused single WhatsApp tab."
            except Exception as e:
                return f"Failed to handle WhatsApp tab: {e}"
                
        if "linkedin.com" in url:
            try:
                await get_single_linkedin_page(context, url)
                return f"✓ Reused and focused single LinkedIn tab."
            except Exception as e:
                return f"Failed to handle LinkedIn tab: {e}"
        
        # Si estamos en Obscura (puerto 9222) y la primera pestaña es about:blank, la reutilizamos
        # para evitar que Playwright cierre las pestañas nuevas temporales al desconectar.
        if cdp_url.endswith("9222") and pages and pages[0].url == "about:blank":
            try:
                await pages[0].goto(url, wait_until="domcontentloaded", timeout=15000)
                return f"✓ Reused and loaded {url} in primary Obscura tab."
            except Exception as e:
                return f"Failed to load in primary tab: {e}"

        existing_page = None
        for pg in pages:
            if pg.url == url:
                existing_page = pg
                break
            try:
                pg_host = urlparse(pg.url).netloc.lower()
                if pg_host and pg_host == target_host:
                    existing_page = pg
                    break
            except Exception:
                pass
                
        if existing_page:
            try:
                await existing_page.bring_to_front()
                if existing_page.url != url:
                    await existing_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                return f"✓ Reused and focused existing tab for {url}."
            except Exception:
                pass
                
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return f"✓ Successfully opened {url} in a new tab."
        except Exception as e:
            return f"Opened tab, but navigation encountered an error: {e}"

def _ensure_interactions_table(cursor):
    """Crea la tabla interactions_history si no existe (idempotente, backward-compatible)."""
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


# ─── Retina Semántica: Cache + Estado en Memoria ─────────────

# Mapa interactivo en memoria — almacena el resultado de get_interactive_map()
_retina_element_map: dict = {}   # { int_id: { selector, tag, role, name, ... } }
_retina_map_url: str = ""
_retina_map_ts: float = 0.0

def _ensure_retina_cache_table(cursor):
    """Crea la tabla retina_cache si no existe (idempotente)."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS retina_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            url          TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            cache_type   TEXT NOT NULL,
            data         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            ttl_seconds  INTEGER DEFAULT 180
        )
    """)
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_retina_url_type ON retina_cache(url, cache_type, created_at)")
    except Exception:
        pass

def _retina_cache_get(url, cache_type, ttl=180):
    """Busca un resultado válido (no expirado) en retina_cache. Retorna str o None."""
    db_path = os.path.join(_db_dir(), "simbad_browser.db")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        _ensure_retina_cache_table(cursor)
        cursor.execute("""
            SELECT data, created_at FROM retina_cache
            WHERE url = ? AND cache_type = ?
            ORDER BY created_at DESC LIMIT 1
        """, (url, cache_type))
        row = cursor.fetchone()
        conn.close()
        if row:
            created = datetime.fromisoformat(row[1])
            if (datetime.now() - created).total_seconds() < ttl:
                return row[0]
    except Exception as e:
        print(f"Retina cache read error: {e}")
    return None

def _retina_cache_set(url, content_hash, cache_type, data, ttl=180):
    """Guarda un resultado en retina_cache."""
    db_path = os.path.join(_db_dir(), "simbad_browser.db")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        _ensure_retina_cache_table(cursor)
        cursor.execute("""
            INSERT INTO retina_cache (url, content_hash, cache_type, data, created_at, ttl_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (url, content_hash, cache_type, data, datetime.now().isoformat(), ttl))
        conn.commit()
        # Purga probabilística: ~10% de las veces limpia entradas expiradas
        if random.random() < 0.1:
            cursor.execute("DELETE FROM retina_cache WHERE datetime(created_at, '+' || ttl_seconds || ' seconds') < datetime('now')")
            # Límite duro: máximo 500 entradas
            cursor.execute("DELETE FROM retina_cache WHERE id NOT IN (SELECT id FROM retina_cache ORDER BY created_at DESC LIMIT 500)")
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Retina cache write error: {e}")

def _retina_invalidate(url=None):
    """Invalida el mapa interactivo en memoria y opcionalmente el caché para una URL."""
    global _retina_element_map, _retina_map_url, _retina_map_ts
    _retina_element_map = {}
    _retina_map_url = ""
    _retina_map_ts = 0.0
    if url:
        db_path = os.path.join(_db_dir(), "simbad_browser.db")
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            _ensure_retina_cache_table(cursor)
            cursor.execute("DELETE FROM retina_cache WHERE url = ?", (url,))
            conn.commit()
            conn.close()
        except Exception:
            pass

def _get_page_content_hash(page_url, page_title):
    """Hash rápido del estado de la página para validación de caché."""
    raw = f"{page_url}|{page_title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def _build_a11y_node(node, depth=0, max_depth=6):
    """Convierte un nodo del snapshot de accesibilidad a texto indentado compacto."""
    if not node or depth > max_depth:
        return ""
    role = node.get("role", "")
    name = node.get("name", "").strip()
    # Filtrar nodos vacíos y redundantes
    if role in ("none", "generic", "presentation") and not name:
        # Procesar hijos directamente sin este nodo
        lines = []
        for child in node.get("children", []):
            lines.append(_build_a11y_node(child, depth, max_depth))
        return "".join(lines)
    if not name and not node.get("children"):
        return ""
    indent = "  " * depth
    extras = []
    if node.get("level"):
        extras.append(f"level={node['level']}")
    if node.get("focused"):
        extras.append("focused")
    if node.get("disabled"):
        extras.append("disabled")
    if node.get("checked") is not None:
        extras.append(f"checked={node['checked']}")
    if node.get("value") and node["value"] != name:
        val = str(node["value"])[:30]
        extras.append(f'value="{val}"')
    extra_str = f" ({', '.join(extras)})" if extras else ""
    name_str = f' "{name}"' if name else ""
    line = f"{indent}[{role}]{name_str}{extra_str}\n"
    for child in node.get("children", []):
        line += _build_a11y_node(child, depth + 1, max_depth)
    return line


def check_already_commented(target_id: str, content_hash: str, platform: str = 'linkedin') -> bool:
    db_dir = _db_dir()
    db_path = os.path.join(db_dir, "simbad_browser.db")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        _ensure_interactions_table(cursor)
        conn.commit()
        
        # 1. Check if content_hash already exists for this identity (duplicate post)
        cursor.execute("""
            SELECT id FROM interactions_history 
            WHERE platform = ? AND content_hash = ?
        """, (platform, content_hash))
        if cursor.fetchone():
            conn.close()
            return True
            
        # 2. Check if we commented on this target_id (author) in the last 24 hours
        #    (la regla de 24h es POR IDENTIDAD: comentar como página no bloquea el perfil personal)
        cursor.execute("""
            SELECT timestamp FROM interactions_history
            WHERE platform = ? AND target_id = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (platform, target_id))
        row = cursor.fetchone()
        if row and row[0]:
            try:
                last_time = datetime.fromisoformat(row[0])
                # Usar hora local en ambos lados: record_comment guarda datetime.now().isoformat()
                if datetime.now() - last_time < timedelta(hours=24):
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

def record_comment(target_id: str, content_hash: str, details: str, post_url: str = "", platform: str = 'linkedin'):
    db_dir = _db_dir()
    db_path = os.path.join(db_dir, "simbad_browser.db")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        _ensure_interactions_table(cursor)
        cursor.execute("""
            INSERT INTO interactions_history (platform, target_id, content_hash, details, post_url, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (platform, target_id, content_hash, details, post_url, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        print(f"  ✓ Recorded interaction in database for '{target_id}' (hash {content_hash[:8]}).")
    except Exception as e:
        print(f"Error recording interaction: {e}")


# ─── LinkedIn: Helpers compartidos para comentarios ─────────────

# JS para enviar el comentario: busca el botón exacto 'Comentar/Publicar/Post'
# dentro del contenedor del post y clickea el último match (el toggle "Comentar"
# del footer también matchea, por eso gana el último). El DOM nuevo ofusca las
# clases CSS, así que el matching es por texto.
_COMMENT_SUBMIT_JS = '''el => {
    const buttons = Array.from(el.querySelectorAll('button'));
    const matching = buttons.filter(b => {
        const txt = b.innerText.trim().toLowerCase();
        return txt === 'comentar' || txt === 'comment' || txt === 'publicar' || txt === 'post';
    });
    const found = matching.length > 0 ? matching[matching.length - 1] : null;
    if (found) {
        found.scrollIntoView({ behavior: 'instant', block: 'center' });
        found.click();
        return true;
    }
    return false;
}'''

# JS genérico para detectar si el usuario ACTIVO (cualquier cuenta, no hardcodeada)
# ya tiene un comentario en el post. Obtiene el nombre desde la foto del menú de
# perfil de la global-nav y lo busca en los comentarios visibles.
_DOM_OWN_COMMENT_JS = '''el => {
    // 1) Obtener el nombre del usuario logueado desde la foto del menú de perfil
    let myName = null;
    const navImg = document.querySelector('.global-nav__me-photo, button[aria-label*="perfil"] img[alt], img[alt][id*="global-nav"][class*="avatar"]');
    if (navImg && navImg.alt && navImg.alt.trim()) myName = navImg.alt.toLowerCase();
    if (!myName) {
        const meArea = document.querySelector('.global-nav__me, nav[class*="global-nav"]');
        if (meArea) {
            const img = meArea.querySelector('img[alt]');
            if (img && img.alt.trim()) myName = img.alt.toLowerCase();
        }
    }
    if (!myName) return false;

    // 2) Buscar el nombre en los comentarios visibles del post
    //    Heurística por texto (primeras 2 palabras) y por botones de eliminación.
    const myFirst = myName.split(' ').slice(0, 2).join(' ');
    const commentBlocks = Array.from(el.querySelectorAll('article, [class*="comment"], [data-id]'));
    for (const block of commentBlocks) {
        const t = (block.innerText || '').toLowerCase();
        if (t.includes(myFirst)) return true;
    }
    const fullText = (el.innerText || '').toLowerCase();
    if (fullText.includes(myName)) return true;

    // Fallback clásico: botones de "Eliminar comentario" (solo el autor puede verlos)
    const deleteBtns = el.querySelectorAll('button[aria-label*="Eliminar comentario"], button[aria-label*="Delete comment"]');
    return deleteBtns.length > 0;
}'''


async def _find_post_in_feed(page, author_name: str, max_attempts: int = 8):
    """Escanea el feed buscando un post cuyo autor coincida con `author_name`
    (match case-insensitive por substring), scrolleando/cargando más posts.

    Retorna (post_index, post_text, clean_author) o (None, None, None)."""
    print(f"Scanning for post by author '{author_name}'...")
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        posts_locator = page.locator(_POSTS_LOCATOR)
        posts_count = await posts_locator.count()
        print(f"Scan attempt {attempt}: Found {posts_count} posts in DOM.")

        for i in range(posts_count):
            item = posts_locator.nth(i)
            text = await item.inner_text()
            # Robust link-based author extraction (shared with get_feed_posts)
            author = await item.evaluate(_AUTHOR_EXTRACT_JS)
            clean_author = _clean_author(author)
            if author_name.lower() in clean_author.lower():
                print(f"  ✓ Found matching author '{clean_author}' at post index {i}.")
                return i, text, clean_author

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

    return None, None, None


async def _open_comment_box(page, post_locator):
    """Asegura que la caja de comentario del post esté visible; si no, la abre
    con el botón 'Comentar' del footer. Retorna el locator de la caja o None."""
    comment_box = post_locator.locator('div[role="textbox"], [contenteditable="true"], .ql-editor').first
    if await comment_box.count() == 0 or not await comment_box.is_visible():
        print("Opening comment editor...")
        comment_btn = post_locator.locator(
            'button[aria-label*="Comentar"], button[aria-label*="Comment"], '
            'button:has-text("Comentar"), button:has-text("Comment")'
        ).first
        if await comment_btn.count() > 0 and await comment_btn.is_visible():
            await comment_btn.evaluate("b => b.click()")
            await asyncio.sleep(2)
    if await comment_box.count() == 0 or not await comment_box.is_visible():
        return None
    return comment_box


async def _click_submit_playwright(post_locator, page=None):
    """Hace click REAL de Playwright (no JS) en el botón de envío del comentario.

    IMPORTANTE (causa raíz): el click via JS evaluator (el.click()) NO dispara los
    manejadores React de LinkedIn en el DOM actual: la caja se colapsaba sin publicar
    nada, y la verificación de 'caja vacía' lo daba por éxito (falso positivo).
    Un click real de Playwright genera los eventos de puntero/ratón que React sí procesa.

    El botón de envío es el ÚLTIMO botón con texto 'Comentar'/'Comment'/'Publicar'/'Post'
    dentro del post (el del composer está después del footer en el DOM)."""
    try:
        submit = post_locator.locator(
            'button:has-text("Comentar"), button:has-text("Comment"), '
            'button:has-text("Publicar"), button:has-text("Post")'
        ).last
        if await submit.count() == 0:
            return False
        # scroll al botón y click real de Playwright
        await submit.scroll_into_view_if_needed(timeout=5000)
        await asyncio.sleep(0.3)
        await submit.click(timeout=8000)
        return True
    except Exception as e:
        print(f"  - Click real de Playwright falló: {e}")
        return False


async def _verify_comment_published(page, post_locator, comment_text: str, comment_box) -> bool:
    """Verificación REAL de publicación: no basta con que la caja se vacíe
    (eso es un falso positivo, la caja colapsa aunque el envío falle).

    Espera hasta 12s a que el texto del comentario aparezca en el DOM de la página
    FUERA de la caja de edición (es decir, en la lista de comentarios publicados)."""
    for check in range(24):  # 24 x 0.5s = 12s
        try:
            box_count = await comment_box.count()
            box_text = ""
            if box_count > 0:
                box_text = (await comment_box.inner_text() or "").strip()
            # El comentario está publicado si el texto aparece en la página y la caja ya no lo contiene
            page_text = await page.evaluate("() => document.body.innerText")
            snippet = comment_text[:60]
            appears_in_page = snippet in page_text
            box_cleared = box_count == 0 or box_text == ""
            if appears_in_page and box_cleared:
                return True
        except Exception:
            # Element detached: probablemente la caja colapsó tras publicar;
            # verificar por el texto en la página
            try:
                page_text = await page.evaluate("() => document.body.innerText")
                if comment_text[:60] in page_text:
                    return True
            except Exception:
                pass
        await asyncio.sleep(0.5)
    return False


async def _type_and_submit_comment(page, post_locator, comment_text: str, comment_box) -> bool:
    """Escribe el comentario (typing humanizado), lo envía con CLICK REAL de Playwright
    y VERIFICA que el comentario apareció publicado en la página (no solo la caja vacía).
    Retorna True si el comentario quedó publicado."""
    print("Typing comment...")
    await comment_box.focus()
    await comment_box.fill("")
    await page.keyboard.type(comment_text, delay=35)
    await asyncio.sleep(1.5)

    # Click REAL de Playwright en el botón de envío (causa raíz: el click JS no publica)
    print("Submitting comment (click real de Playwright)...")
    submit_success = await _click_submit_playwright(post_locator)

    if not submit_success:
        # Fallback: reintento con el evaluator JS como plan B
        print("  - Fallback: reintentando con evaluator JS...")
        submit_success = await post_locator.evaluate(_COMMENT_SUBMIT_JS)

    if not submit_success:
        return False

    print("  - Clicked submit button. Verifying real publication...")
    is_successful = await _verify_comment_published(page, post_locator, comment_text, comment_box)

    if not is_successful:
        print("  - Comment not detected in page. Retrying submit click...")
        await _click_submit_playwright(post_locator)
        await asyncio.sleep(3)
        is_successful = await _verify_comment_published(page, post_locator, comment_text, comment_box)

    return is_successful


@mcp.tool()
async def get_feed_posts(limit: int = 5) -> str:
    """
    Scans the LinkedIn feed and returns a list of posts (author, text, post_url) that have NOT
    been commented on today. `post_url` is the permalink of the post (may be empty if not found).
    Use this to read posts before generating specific, tailored comments.
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            contexts = browser.contexts
            if not contexts: return "Error: No browser contexts."
            context = contexts[0]
            try:
                page = await get_single_linkedin_page(context)
            except Exception as e:
                return f"Error: Could not get LinkedIn page: {e}"
            
            results = []
            seen_hashes = set()
            
            for attempt in range(8):
                posts = await page.locator(_POSTS_LOCATOR).all()
                for post in posts:
                    if len(results) >= limit: break
                    text = await post.inner_text()
                    if not text: continue

                    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    if content_hash in seen_hashes: continue
                    seen_hashes.add(content_hash)
                    
                    author = await post.evaluate(_AUTHOR_EXTRACT_JS)
                    clean_author = _clean_author(author)

                    if clean_author == "Unknown": continue

                    # Check DB
                    if not check_already_commented(clean_author, content_hash):
                        post_url = await post.evaluate(_POST_URL_EXTRACT_JS)
                        results.append({
                            "author": clean_author,
                            "text": text[:500] + "..." if len(text) > 500 else text,
                            "post_url": post_url,
                        })
                        
                if len(results) >= limit: break
                
                # Scroll incremental del contenedor del feed (main) para disparar
                # el lazy loading. Un salto directo a scrollHeight NO carga posts nuevos.
                await page.evaluate("""() => {
                    const el = document.getElementById("workspace") || document.querySelector("main");
                    if (el) el.scrollTop += 1200;
                }""")
                await asyncio.sleep(2.5)
                
                # Try clicking "Cargar más" / "Load more" button
                load_more = page.locator("button:has-text('Cargar más'), button:has-text('Load more')").first
                if await load_more.count() == 0:
                    load_more = page.get_by_text("Cargar más").first
                if await load_more.count() == 0:
                    load_more = page.get_by_text("Load more").first
                    
                if await load_more.count() > 0:
                    try:
                        await load_more.click(timeout=3000)
                        await asyncio.sleep(4)
                    except Exception:
                        pass
                
            return json.dumps(results, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"Error: {e}"

@mcp.tool()
async def comment_on_post(author_name: str, comment_text: str) -> str:
    """
    Type and submit a comment on a LinkedIn post in the active browser tab.
    Locates the post matching the author's name, checks local database
    to avoid duplicate commenting, types the comment, and clicks submit.

    REGLA MANDATORIA DE CALIDAD: Prohibido comentarios genéricos en lote o plantillas
    repetitivas (ej. 'Totalmente de acuerdo...', 'Excelente aporte...'). El comentario debe
    ser 100% único y analizar específicamente la tesis de la publicación del autor.
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
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
        try:
            page = await get_single_linkedin_page(context)
        except Exception as e:
            return f"Error: Could not get LinkedIn page: {e}"
            
        # Ensure we are on the feed page
        if "linkedin.com/feed" not in page.url:
            print(f"Current URL is {page.url}. Navigating to LinkedIn feed...")
            await page.goto("https://www.linkedin.com/feed/")
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)
            
        # 1. Find the post item matching the author, scrolling if necessary
        target_index, target_text, target_clean_author = await _find_post_in_feed(page, author_name)

        if target_index is None:
            return f"Error: Post by author '{author_name}' was not found in the feed after scrolling."
            
        # 2. Database guard check to prevent duplication
        content_hash = hashlib.sha256(target_text.encode("utf-8")).hexdigest()
        
        if check_already_commented(target_clean_author, content_hash):
            return f"Warning: Post by author '{target_clean_author}' (hash '{content_hash[:8]}') has already been commented on. Skipping to prevent duplicate."
            
        # 3. Interact with target post
        post_locator = page.locator(_POSTS_LOCATOR).nth(target_index)
        
        # Center the post container to prevent sticky header occlusion and ensure visibility
        print("Centering post in viewport...")
        await post_locator.evaluate("el => el.scrollIntoView({ behavior: 'instant', block: 'center' })")
        await asyncio.sleep(2)
        
        # Extract post URL link (shared with get_feed_posts)
        post_url = await post_locator.evaluate(_POST_URL_EXTRACT_JS)
        print(f"  - Extracted post URL: {post_url}")
        
        comment_box = await _open_comment_box(page, post_locator)
        
        # Check the DOM to see if the active user already commented
        already_commented_in_dom = await post_locator.evaluate(_DOM_OWN_COMMENT_JS)
        
        if already_commented_in_dom:
            print("  ✓ Found existing comment by active user in DOM. Aborting to prevent duplicate.")
            return f"Warning: You already have a comment on this post in the DOM. Aborted to prevent duplicate."
            
        if comment_box is None:
            return "Error: Could not open the comment editor for this post."
            
        # 4-5. Focus, type and submit comment (shared helper)
        success = await _type_and_submit_comment(page, post_locator, comment_text, comment_box)

        if not success:
            return "Error: Comment box did not clear after multiple submission attempts."

        # Record success in the database
        record_comment(target_clean_author, content_hash, comment_text, post_url)
        return f"✓ Comment successfully posted on {author_name}'s post."

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
async def publish_linkedin_post(content: str, linkedin_media_path: str = "") -> str:
    """
    Create and publish a new post on LinkedIn in the active browser tab (personal profile).
    Supports text-only posts as well as image or video file uploads.
    
    Args:
        content: Text content of the post.
        linkedin_media_path: (Optional) Absolute path to an image or video file inside the container (e.g. '/app/media/video.mp4').
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()
    
    if linkedin_media_path and not os.path.exists(linkedin_media_path):
        return f"Error: Media file not found at path: {linkedin_media_path}"

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            return f"Error: Could not connect to Obscura on {cdp_url}. Details: {e}"

        contexts = browser.contexts
        if not contexts:
            return "Error: No browser contexts found."
        context = contexts[0]
        try:
            page = await get_single_linkedin_page(context)
        except Exception as e:
            return f"Error: Could not get LinkedIn page: {e}"

        # Navigate to clean feed URL (without query params that trigger overlays)
        clean_feed = "https://www.linkedin.com/feed/"
        if page.url != clean_feed:
            print(f"Current: {page.url} → navigating to clean feed...")
            await page.goto(clean_feed)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)

        # ---- STEP 1: OPEN DIALOG ----
        print("Step 1: Opening share post dialog...")
        # El trigger "Crear publicación" está en el DOM principal; el diálogo se
        # renderiza dentro del shadow DOM (#interop-outlet). Usamos JS para
        # localizar y disparar el click correctamente.
        dialog_opened = await page.evaluate("""async () => {
            const draft = document.getElementById('draft-text-replaceable-component');
            let trigger = null;
            if (draft) {
                let cur = draft;
                for (let i = 0; i < 6; i++) {
                    cur = cur.parentElement;
                    if (cur && cur.getAttribute('role') === 'button') { trigger = cur; break; }
                }
            }
            if (!trigger) {
                // Fallback: buscar cualquier elemento con texto "Crear publicación"
                const els = Array.from(document.querySelectorAll('[role="button"], button, a, div'));
                trigger = els.find(el => {
                    const t = (el.innerText || '').trim();
                    return t.startsWith('Crear publicación') && t.length < 80 && el.children.length < 8;
                });
            }
            if (!trigger) return 'NO_TRIGGER';
            trigger.scrollIntoView({ behavior: 'instant', block: 'center' });
            await new Promise(r => setTimeout(r, 300));
            const r = trigger.getBoundingClientRect();
            const opts = { bubbles: true, cancelable: true, view: window,
                           clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, detail: 1 };
            trigger.dispatchEvent(new MouseEvent('mousedown', opts));
            trigger.dispatchEvent(new MouseEvent('mouseup', opts));
            trigger.dispatchEvent(new MouseEvent('click', opts));
            return 'CLICKED';
        }""")
        if dialog_opened == 'NO_TRIGGER':
            # Fallback: selector clásico con texto (Playwright atraviesa)
            share_box = page.locator('button:has-text("Crear publicación"), button:has-text("Start a post"), [aria-label*="Crear publicación"]').first
            if await share_box.count() > 0 and await share_box.is_visible():
                await share_box.click(force=True)
            else:
                return "Error: No se encontró el botón 'Crear publicación' en el feed."
        await asyncio.sleep(4)

        # ---- STEP 2: HANDLE MEDIA UPLOAD (IF PROVIDED) ----
        if linkedin_media_path and os.path.exists(linkedin_media_path):
            print(f"Step 2: Uploading media file: {linkedin_media_path}")
            # El botón "Añadir contenido" vive en el shadow DOM del diálogo
            media_clicked = await page.evaluate("""() => {
                const root = document.querySelector('#interop-outlet')?.shadowRoot;
                if (!root) return 'NO_SHADOW';
                const btns = Array.from(root.querySelectorAll('button'));
                const btn = btns.find(b => (b.getAttribute('aria-label') || '').toLowerCase().includes('a\u00f1adir contenido'));
                if (!btn) return 'NO_BTN';
                btn.click();
                return 'CLICKED';
            }""")
            if media_clicked in ('CLICKED',):
                async with page.expect_file_chooser() as fc_info:
                    # Ya clickeado; esperar el file chooser
                    await asyncio.sleep(0.5)
                file_chooser = await fc_info.value
                await file_chooser.set_files(linkedin_media_path)
                print("  ✓ File uploaded via file chooser")
                await asyncio.sleep(8)

                # Accept video upload (Clicking Siguiente/Next/Listo) — también en shadow
                next_result = await page.evaluate("""async () => {
                    for (let i = 0; i < 15; i++) {
                        const root = document.querySelector('#interop-outlet')?.shadowRoot;
                        if (root) {
                            const btns = Array.from(root.querySelectorAll('button'));
                            const n = btns.find(b => {
                                const t = b.innerText.trim();
                                return t === 'Siguiente' || t === 'Next' || t === 'Listo';
                            });
                            if (n && !n.disabled) { n.click(); return 'CLICKED'; }
                        }
                        await new Promise(r => setTimeout(r, 1000));
                    }
                    return 'TIMEOUT';
                }""")
                if next_result == 'CLICKED':
                    await asyncio.sleep(5)
            else:
                print("  ⚠ Botón 'Añadir contenido' no encontrado; continuando solo texto.")

        # ---- STEP 3: TYPE CONTENT ----
        print("Step 3: Locating editor and typing content...")
        # El editor está en el shadow DOM. Lo enfocamos por JS y escribimos
        # con el teclado nativo (el foco queda dentro del shadow root).
        editor_ready = await page.evaluate("""async () => {
            for (let i = 0; i < 10; i++) {
                const root = document.querySelector('#interop-outlet')?.shadowRoot;
                if (root) {
                    const ed = root.querySelector('.ql-editor, [contenteditable="true"], div[role="textbox"]');
                    if (ed) { ed.focus(); ed.click(); return 'FOCUSED'; }
                }
                await new Promise(r => setTimeout(r, 500));
            }
            return 'NO_EDITOR';
        }""")
        if editor_ready != 'FOCUSED':
            return "Error: Could not locate text editor box in share dialog (shadow DOM)."
        await human_type(page.keyboard, content)
        print("  ✓ Content typed successfully")
        await asyncio.sleep(3)

        # ---- STEP 4: PUBLISH ----
        print("Step 4: Locating Publish button...")
        publish_ready = await page.evaluate("""async () => {
            for (let i = 0; i < 20; i++) {
                const root = document.querySelector('#interop-outlet')?.shadowRoot;
                if (root) {
                    const btns = Array.from(root.querySelectorAll('button'));
                    const pub = btns.find(b => {
                        const t = b.innerText.trim();
                        return t === 'Publicar' || t === 'Post' || t === 'Publish';
                    });
                    if (pub && !pub.disabled) return 'ENABLED';
                }
                await new Promise(r => setTimeout(r, 1000));
            }
            return 'DISABLED_OR_MISSING';
        }""")
        if publish_ready != 'ENABLED':
            return "Error: Publish button remained disabled or missing (shadow DOM)."

        print("Publishing...")
        pub_clicked = await page.evaluate("""() => {
            const root = document.querySelector('#interop-outlet')?.shadowRoot;
            if (!root) return 'NO_SHADOW';
            const btns = Array.from(root.querySelectorAll('button'));
            const pub = btns.find(b => {
                const t = b.innerText.trim();
                return t === 'Publicar' || t === 'Post' || t === 'Publish';
            });
            if (pub && !pub.disabled) { pub.click(); return 'PUBLISHED'; }
            return 'NOT_READY';
        }""")
        await asyncio.sleep(10)

        # ---- STEP 5: VERIFY ----
        dialog_count = await page.evaluate("""() => {
            const main = document.querySelectorAll('[role="dialog"]').length;
            const root = document.querySelector('#interop-outlet')?.shadowRoot;
            const shadow = root ? Array.from(root.querySelectorAll('[role="dialog"]')).filter(d => {
                const b = d.getBoundingClientRect();
                return b.width > 100 && b.height > 100;
            }).length : 0;
            return main + shadow;
        }""")
        if pub_clicked == 'PUBLISHED' and dialog_count == 0:
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            record_comment("self_publish", content_hash, content, "https://www.linkedin.com/feed/")
            print("  ✓ Post published successfully!")
            return "✓ Post successfully published on LinkedIn!"
        else:
            await page.screenshot(path="/app/media/linkedin_publish_final.png")
            return "Warning: Post submitted. Check '/app/media/linkedin_publish_final.png' to verify if modal closed."


# ─── LinkedIn: Páginas Gestionadas ──────────────────────────

# Caché de sesión: la lista de páginas gestionadas NO cambia durante la sesión.
# Resolverla una sola vez evita depender de la renderización intermitente de la
# sección 'Gestionar' del feed en cada llamada (causa de fallos intermitentes).
_managed_companies_cache: dict = None

async def _fetch_managed_companies_from_me_menu(page) -> dict:
    """Extrae las páginas gestionadas desde el MENÚ 'Yo' (más fiable que la
    sección lateral del feed). Abre el menú de perfil y recoge los links
    /company/ listados. Retorna el mismo formato que _fetch_managed_companies."""
    try:
        # Cerrar skip-menu si está visible
        await page.evaluate("""() => {
            const b = [...document.querySelectorAll('button')].find(x =>
                /menú de omisión|skip/i.test(x.getAttribute('aria-label') || ''));
            if (b) { const r = b.getBoundingClientRect(); if (r.width > 0 && r.height > 0) b.click(); }
        }""")
        await asyncio.sleep(1)

        # Abrir el menú 'Yo' (trigger: div[role=button] con texto 'Yo' a la derecha)
        opened = await page.evaluate("""() => {
            const me = [...document.querySelectorAll('div[role="button"]')].find(d => {
                const t = (d.innerText || '');
                const r = d.getBoundingClientRect();
                return /\\nYo\\s*$|^Yo$/m.test(t.trim()) || (t.includes('Yo') && r.width > 0 && r.x > 900);
            });
            if (!me) return false;
            me.click();
            return true;
        }""")
        if not opened:
            return {"error": "No se pudo abrir el menú 'Yo'.", "managed_pages": []}
        await asyncio.sleep(2)

        # Extraer links de empresa dentro del menú (DOM normal + shadow root)
        result = await page.evaluate("""() => {
            const roots = [document];
            const interop = document.querySelector('#interop-outlet');
            if (interop && interop.shadowRoot) roots.push(interop.shadowRoot);
            const seen = new Set();
            const companies = [];
            for (const root of roots) {
                for (const a of root.querySelectorAll('a[href*="/company/"]')) {
                    const name = (a.innerText || '').trim();
                    const url = a.href;
                    if (!name || name.length > 120 || seen.has(url)) continue;
                    seen.add(url);
                    companies.push({name: name, url: url});
                }
            }
            return {declaredCount: companies.length, actualCount: companies.length, companies: companies};
        }""")

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        if result.get("companies"):
            return result
        return {"error": "El menú 'Yo' no mostró páginas de empresa.", "managed_pages": []}
    except Exception as e:
        print(f"Fallback menú Yo falló: {e}")
        return {"error": f"Error en fallback menú Yo: {e}", "managed_pages": []}


async def _fetch_managed_companies(page) -> dict:
    """Descubre todas las páginas de empresa gestionadas por el perfil logueado.
    Usa caché de sesión. Método principal: sección 'Gestionar' del feed.
    Fallback: menú 'Yo' (más fiable cuando la sección lateral no renderiza).

    Retorna: {'companies': [...], 'declaredCount': N, 'actualCount': N}
             o {'error': ...} si no se encuentra la sección."""
    global _managed_companies_cache
    if _managed_companies_cache is not None:
        print(f"  [caché] Usando lista de {len(_managed_companies_cache.get('companies', []))} páginas gestionadas.")
        return _managed_companies_cache

    # ── Método principal: navegar al feed y buscar la sección 'Gestionar' ──
    if "linkedin.com/feed" not in page.url:
        print("Navigating to LinkedIn feed...")
        await page.goto("https://www.linkedin.com/feed/")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(4)

    # ── 1. Detectar la sección "Gestionar (N)" / "Manage" / "Páginas" ──
    print("Looking for managed pages section...")
    has_section = await page.evaluate("""() => {
        return [...document.querySelectorAll('*')].some(el =>
            el.children.length === 0 &&
            /(Gestionar|Manage|Páginas|Pages)/i.test(el.textContent.trim())
        );
    }""")

    if not has_section:
        await asyncio.sleep(3)
        has_section = await page.evaluate("""() => {
            return [...document.querySelectorAll('*')].some(el =>
                el.children.length === 0 &&
                /(Gestionar|Manage|Páginas|Pages)/i.test(el.textContent.trim())
            );
        }""")

    if not has_section:
        print("Sección 'Gestionar' no encontrada → usando fallback del menú 'Yo'.")
        result = await _fetch_managed_companies_from_me_menu(page)
        if "error" not in result and result.get("companies"):
            _managed_companies_cache = result
            return result
        return {
            "error": "No se encontró la sección 'Gestionar' en LinkedIn.",
            "hint": "Asegúrate de haber iniciado sesión y gestionar al menos una página de empresa.",
            "managed_pages": []
        }

    # ── 2. Expandir "Mostrar todo" si existe ──
    show_all = page.locator("a:has-text('Mostrar todo'), button:has-text('Mostrar todo')").first
    if await show_all.count() > 0:
        try:
            await show_all.click(timeout=3000)
            await asyncio.sleep(2)
            print("  ✓ Clicked 'Mostrar todo'")
        except Exception:
            pass

    # ── 3. Clic repetido en "Cargar más" hasta que desaparezca ──
    for _ in range(5):
        load_more = page.locator("a:has-text('Cargar más'), button:has-text('Cargar más')").first
        if await load_more.count() > 0:
            try:
                await load_more.click(timeout=3000)
                await asyncio.sleep(2)
                print("  ✓ Clicked 'Cargar más'")
            except Exception:
                break
        else:
            break

    # ── 4. Extraer las empresas ──
    result = await page.evaluate("""() => {
        const allElements = [...document.querySelectorAll('*')];
        const header = allElements.find(el =>
            el.children.length === 0 &&
            /Gestionar/.test(el.textContent.trim())
        );

        if (!header) return {error: "La sección desapareció después de expandir"};

        const countMatch = header.textContent.trim().match(/\\((\\d+)\\)/);
        const declaredCount = countMatch ? parseInt(countMatch[1]) : 0;

        let container = header.parentElement;
        let companyLinks = [];
        for (let i = 0; i < 10; i++) {
            companyLinks = container.querySelectorAll('a[href*="/company/"]');
            if (companyLinks.length > 0) break;
            container = container.parentElement;
        }

        const companies = [...companyLinks]
            .filter(a => {
                const text = a.textContent.trim();
                return text.length > 1 && text.length < 120;
            })
            .map(a => ({
                name: a.textContent.trim(),
                url: a.href
            }));

        const seen = new Set();
        const unique = companies.filter(c => {
            if (seen.has(c.url)) return false;
            seen.add(c.url);
            return true;
        });

        return {
            declaredCount,
            actualCount: unique.length,
            companies: unique
        };
    }""")
    # Guardar en caché de sesión: la lista no cambia entre llamadas
    if "error" not in result and result.get("companies"):
        _managed_companies_cache = result
    return result


def _company_slug_from_url(url: str) -> str:
    """Extrae el slug de una URL tipo https://www.linkedin.com/company/<slug>/..."""
    m = re.search(r"/company/([^/?#]+)", url or "")
    return m.group(1) if m else (url or "unknown")


@mcp.tool()
async def get_linkedin_managed_pages() -> str:
    """
    Extrae todas las páginas de empresa de LinkedIn gestionadas por el perfil logueado.
    Navega al feed, localiza la sección "Gestionar (N)", expande la lista completa
    (clic en "Mostrar todo" y "Cargar más" si existen), y devuelve nombre y URL
    de cada página administrada.

    Requiere haber iniciado sesión en LinkedIn (usa el motor 'chromium').
    Funciona con CUALQUIER cuenta logueada, no solo la del desarrollador.
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                return f"Error: Could not connect to browser on {cdp_url}. Details: {e}"

            contexts = browser.contexts
            if not contexts:
                return "Error: No browser contexts found."
            context = contexts[0]

            try:
                page = await get_single_linkedin_page(context)
            except Exception as e:
                return f"Error: Could not get LinkedIn page: {e}"

            result = await _fetch_managed_companies(page)
            return json.dumps(result, indent=2, ensure_ascii=False)

    except Exception as e:
        return f"Error inesperado: {e}"


# ─── LinkedIn: Solicitudes de Conexión ───────────────────────

@mcp.tool()
async def send_linkedin_connection_requests(limit: int = 5, keywords: str = "") -> str:
    """
    Navega a la sección de sugerencias de contactos en LinkedIn (https://www.linkedin.com/mynetwork/grow/)
    y envía solicitudes de conexión a los perfiles recomendados ("Gente que podrías conocer").
    Evita duplicados usando la base de datos local para no repetir invitaciones.

    Args:
        limit: Número máximo de solicitudes a enviar en esta llamada (default: 5, máx recomendado: 10 por sesión).
        keywords: (Opcional) Término de búsqueda para filtrar sugerencias por cargo, empresa o intereses
                  (ej: "IA, CEO, Gerente, Industrial, Founder"). Si se omite, envía invitaciones a las mejores sugerencias del feed.
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            contexts = browser.contexts
            if not contexts: return "Error: No browser contexts."
            context = contexts[0]
            try:
                page = await get_single_linkedin_page(context, "https://www.linkedin.com/mynetwork/grow/")
            except Exception as e:
                return f"Error: Could not get LinkedIn page: {e}"
            
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)
            
            keyword_list = [k.strip().lower() for k in keywords.split(",") if k.strip()]
            results = []
            processed_in_session = set()
            
            for attempt in range(60):
                if len(results) >= limit: break
                loc = page.locator('button[aria-label*="Invita a"], button[aria-label*="Invite"], button:has-text("Conectar"), button:has-text("Connect")')
                count = await loc.count()
                found_new = False
                
                for i in range(count):
                    if len(results) >= limit: break
                    try:
                        btn = loc.nth(i)
                        if await btn.count() == 0: continue
                        aria = await btn.get_attribute("aria-label") or ""
                        txt = await btn.inner_text() or ""
                        if not ("conectar" in aria.lower() or "connect" in aria.lower() or "invita" in aria.lower() or "invite" in aria.lower() or txt.strip().lower() in ["conectar", "connect"]):
                            continue
                            
                        card_text = await btn.evaluate("""el => {
                            let curr = el;
                            while (curr && curr.parentElement && curr.parentElement.tagName !== "BODY") {
                                curr = curr.parentElement;
                                if (curr.innerText && curr.innerText.length > 50 && (curr.innerText.includes("Conectar") || curr.innerText.includes("Connect"))) {
                                    return curr.innerText.trim();
                                }
                            }
                            return el.innerText.trim();
                        }""")
                        
                        name_match = re.search(r'(?:Invita a|Invite)\s+(.*?)\s+(?:a conectar|to connect)', aria, re.IGNORECASE)
                        person_name = name_match.group(1) if name_match else aria.replace("Invita a", "").replace("a conectar", "").strip()
                        if not person_name or person_name.lower() in ["conectar", "connect"]:
                            person_name = "Contacto Sugerido"

                        if person_name in processed_in_session:
                            continue
                        processed_in_session.add(person_name)

                        # Filter by keywords if specified
                        if keyword_list:
                            match_found = any(kw in card_text.lower() or kw in person_name.lower() for kw in keyword_list)
                            if not match_found: continue
                            
                        # Check DB duplicate
                        content_hash = hashlib.sha256((person_name + card_text[:100]).encode("utf-8")).hexdigest()
                        if check_already_commented(f"connect_{person_name}", content_hash):
                            continue
                            
                        found_new = True

                        # Click connect
                        await btn.evaluate("b => b.click()")
                        await asyncio.sleep(2)
                        
                        # Handle optional modal (Send without note / Enviar sin nota)
                        modal = page.locator('[role="dialog"]').first
                        if await modal.count() > 0 and await modal.is_visible():
                            send_without_note = modal.locator('button[aria-label*="sin nota"], button[aria-label*="without a note"], button:has-text("Enviar sin nota"), button:has-text("Send without a note")').first
                            if await send_without_note.count() > 0 and await send_without_note.is_visible():
                                await send_without_note.click()
                                await asyncio.sleep(1.5)
                            else:
                                close_btn = modal.locator('button[aria-label*="Cerrar"], button[aria-label*="Close"], button[aria-label*="Descartar"]').first
                                if await close_btn.count() > 0 and await close_btn.is_visible():
                                    await close_btn.click()
                                    await asyncio.sleep(1)
                                    
                        record_comment(f"connect_{person_name}", content_hash, f"Solicitud de conexión enviada a {person_name}", "https://www.linkedin.com/mynetwork/grow/")
                        results.append({
                            "name": person_name,
                            "card_info": card_text[:200].replace("\n", " | "),
                            "status": "Solicitud enviada"
                        })
                        await asyncio.sleep(2)
                        break # break inner loop to refresh DOM locators
                    except Exception as item_e:
                        print(f"Error connecting item: {item_e}")
                        continue
                
                if not found_new or attempt % 3 == 0:
                    # Scroll down to load more recommendations
                    await page.evaluate("window.scrollBy(0, 800)")
                    await asyncio.sleep(2.5)
                
            return json.dumps(results, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"Error: {e}"


@mcp.tool()
async def send_linkedin_profile_connection(profile_url: str, note: str = "") -> str:
    """
    Navega al perfil específico de LinkedIn de una persona y le envía una solicitud de conexión,
    opcionalmente adjuntando una nota personalizada.

    Args:
        profile_url: URL del perfil de LinkedIn (ej. 'https://www.linkedin.com/in/nombre-apellido/')
        note: (Opcional) Mensaje/nota personalizada para incluir en la solicitud de conexión (máx 300 caracteres).
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            contexts = browser.contexts
            if not contexts: return "Error: No browser contexts."
            context = contexts[0]
            
            vanity = profile_url.strip().rstrip("/").split("/")[-1].split("?")[0]
            invite_url = f"https://www.linkedin.com/preload/custom-invite/?vanityName={vanity}"
            try:
                page = await get_single_linkedin_page(context, invite_url)
            except Exception:
                page = await get_single_linkedin_page(context, profile_url)
            
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
            
            # Check if custom-invite dialog loaded
            add_note_btn = page.locator('button:has-text("Añadir una nota"), button:has-text("Add a note")').first
            if not await add_note_btn.is_visible():
                # Fallback to header buttons on profile page
                if invite_url in page.url:
                    await page.goto(profile_url, wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                
                # Check for direct connect or More menu
                connect_btn = page.locator('a[aria-label*="Invita a"], button[aria-label*="Invita a"], a[href*="/custom-invite/"], button:has-text("Conectar"), button:has-text("Connect")').first
                if not await connect_btn.is_visible():
                    more_btn = page.locator('button:has-text("Más"), button[aria-label="Más"]').first
                    if await more_btn.is_visible():
                        await more_btn.click()
                        await asyncio.sleep(1.5)
                        connect_btn = page.locator('a[aria-label*="Invita a"], button[aria-label*="Invita a"], a[href*="/custom-invite/"], button:has-text("Conectar"), button:has-text("Connect")').first
                
                if await connect_btn.is_visible():
                    await connect_btn.click()
                    await asyncio.sleep(2)
            
            # Handle note entry
            if note:
                add_note_btn = page.locator('button:has-text("Añadir una nota"), button:has-text("Add a note")').first
                if await add_note_btn.is_visible():
                    await add_note_btn.click()
                    await asyncio.sleep(1.5)
                    
                textarea = page.locator('textarea[name="message"], textarea').first
                if await textarea.is_visible():
                    await textarea.focus()
                    await textarea.type(note[:300], delay=10)
                    await asyncio.sleep(1)
                    send_btn = page.locator('button[aria-label*="Enviar invitación"], button:has-text("Enviar"), button:has-text("Send")').first
                    if await send_btn.is_visible():
                        await send_btn.click(force=True)
                        await asyncio.sleep(2)
                        record_comment(f"connect_{profile_url}", hashlib.sha256(note.encode("utf-8")).hexdigest(), note, profile_url)
                        return f"✓ Solicitud de conexión enviada con nota personalizada a {profile_url}."
            
            # Send without note fallback
            send_without_note = page.locator('button[aria-label*="sin nota"], button[aria-label*="without a note"], button:has-text("Enviar sin nota"), button:has-text("Send without a note")').first
            if await send_without_note.is_visible():
                await send_without_note.click()
                await asyncio.sleep(2)
                record_comment(f"connect_{profile_url}", hashlib.sha256(profile_url.encode("utf-8")).hexdigest(), "Sin nota", profile_url)
                return f"✓ Solicitud de conexión enviada sin nota a {profile_url}."
                    
            record_comment(f"connect_{profile_url}", hashlib.sha256(profile_url.encode("utf-8")).hexdigest(), "Sin nota", profile_url)
            return f"✓ Solicitud de conexión enviada a {profile_url}."
        except Exception as e:
            return f"Error: {e}"


# ─── LinkedIn: Publicar en Página de Empresa ─────────────

@mcp.tool()
async def publish_linkedin_company_post(company_url: str, content: str, linkedin_media_path: str = "") -> str:
    """
    Crea y publica un post en una página de empresa de LinkedIn que el perfil
    logueado administra. Soporta texto, imágenes y vídeos.

    Args:
        company_url: URL de la página de empresa (ej. 'https://www.linkedin.com/company/aillu-app/')
        content: Contenido del post (texto plano)
        linkedin_media_path: (Opcional) Ruta a un archivo de imagen/vídeo dentro del contenedor
                    (ej. '/app/media/spot_aillu.mp4'). Si se omite, publica solo texto.
    
    Requiere sesión iniciada en LinkedIn y permisos de administrador en la página.
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()
    
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                return f"Error: Could not connect to browser on {cdp_url}. Details: {e}"

            contexts = browser.contexts
            if not contexts:
                return "Error: No browser contexts found."
            context = contexts[0]

            try:
                page = await get_single_linkedin_page(context)
            except Exception as e:
                return f"Error: Could not get LinkedIn page: {e}"

            # ── 1. Navegar a la página de empresa ──
            print(f"Navigating to company page: {company_url}")
            await page.goto(company_url)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(4)

            current_url = page.url
            print(f"  Current URL: {current_url}")

            if "/admin/" not in current_url:
                return json.dumps({
                    "error": "No se pudo acceder al panel de administración.",
                    "hint": "Asegúrate de ser administrador de esta página y tener sesión iniciada.",
                    "url": current_url
                }, indent=2, ensure_ascii=False)

            # ── 2. Si hay media, flujo con upload ──
            if linkedin_media_path and os.path.exists(linkedin_media_path):
                print(f"Media file detected: {linkedin_media_path}")
                
                # Ir a la página de publicaciones (donde el botón Vídeo sí funciona)
                posts_url = current_url.split('/admin/')[0] + '/admin/page-posts/published/'
                print(f"  Navigating to posts page: {posts_url}")
                await page.goto(posts_url)
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(4)

                # Click "Vídeo" (el de la página de publicaciones, no el video-detour-btn)
                print("  Clicking 'Vídeo' button...")
                await page.evaluate("""() => {
                    const items = [...document.querySelectorAll('button, a, span, li')];
                    const video = items.find(el => {
                        const t = el.textContent.trim();
                        return t === 'V\u00eddeo' || t === 'Video';
                    });
                    if (video) video.click();
                }""")
                await asyncio.sleep(2)

                # Esperar a que aparezca el input[type=file]
                file_input_found = False
                for attempt in range(10):
                    count = await page.evaluate("() => document.querySelectorAll('input[type=file]').length")
                    if count > 0:
                        file_input_found = True
                        break
                    await asyncio.sleep(1)

                if not file_input_found:
                    return "Error: No se encontró el input de archivo después de hacer click en Vídeo."

                # Subir el archivo
                print(f"  Uploading file: {linkedin_media_path}")
                file_input = page.locator('input[type=file]').first
                await file_input.set_input_files(linkedin_media_path)
                print("  ✓ File uploaded")

                # Esperar a que aparezca el editor de vídeo (Siguiente button)
                await asyncio.sleep(5)
                next_found = False
                for attempt in range(15):
                    next_btn = await page.evaluate("""() => {
                        const btns = [...document.querySelectorAll('button')];
                        const n = btns.find(b => b.textContent.trim() === 'Siguiente');
                        return n ? !n.disabled : false;
                    }""")
                    if next_btn:
                        next_found = True
                        break
                    await asyncio.sleep(2)

                if next_found:
                    print("  Clicking 'Siguiente'...")
                    await page.evaluate("""() => {
                        const btns = [...document.querySelectorAll('button')];
                        const n = btns.find(b => b.textContent.trim() === 'Siguiente');
                        if (n) n.click();
                    }""")
                    await asyncio.sleep(5)
                else:
                    print("  'Siguiente' not found - video might still be processing")
                    await asyncio.sleep(10)

            else:
                # ── Flujo sin media: texto desde el dashboard ──
                print("Clicking 'Crear publicación'...")
                clicked = await page.evaluate("""() => {
                    const all = [...document.querySelectorAll('span')];
                    const span = all.find(s => s.textContent.trim() === 'Crear publicaci\u00f3n');
                    if (span) { span.click(); return 'clicked'; }
                    const btns = [...document.querySelectorAll('button, a')];
                    const found = btns.find(b => b.textContent.includes('Crear publicaci'));
                    if (found) { found.click(); return 'fallback'; }
                    return 'not found';
                }""")

                if clicked == 'not found':
                    return f"Error: No se encontró el botón 'Crear publicación' en {current_url}"

                await asyncio.sleep(4)
                print(f"  Post creation URL: {page.url}")

            # ── 3. Esperar al editor ──
            editor_found = False
            for attempt in range(10):
                try:
                    editor = page.get_by_role("textbox").first
                    if await editor.count() > 0:
                        editor_found = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)

            if not editor_found:
                return "Error: No se encontró el editor de texto después de 10s."

            # ── 4. Escribir contenido ──
            print(f"Typing content ({len(content)} chars)...")
            await editor.click()
            await asyncio.sleep(0.5)
            await editor.fill(content)
            await asyncio.sleep(2)

            # ── 5. Publicar ──
            print("Publishing...")
            pub_result = await page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')];
                const pub = btns.find(b => b.textContent.trim() === 'Publicar');
                if (pub && !pub.disabled) {
                    pub.click();
                    return 'PUBLISHED';
                }
                if (pub && pub.disabled) {
                    return 'DISABLED - necesita texto o permisos insuficientes';
                }
                return 'NOT FOUND';
            }""")

            if 'PUBLISHED' not in pub_result:
                return f"Error al publicar: {pub_result}"

            # ── 6. Verificar ──
            await asyncio.sleep(5)
            body = await page.evaluate("() => document.body.innerText")
            if "Se ha publicado" in body or "ha publicado" in body or "Carga completa" in body:
                print("  ✓ Post published successfully!")
                media_tag = " [con media]" if linkedin_media_path and os.path.exists(linkedin_media_path) else ""
                return f"✓ Post publicado exitosamente{media_tag} en la página de empresa.\nURL después de publicar: {page.url}"
            else:
                return f"⚠ Post posiblemente publicado, pero no se pudo confirmar.\nURL: {page.url}"

    except Exception as e:
        return f"Error inesperado: {e}"


# ─── LinkedIn: Comentar como Página de Empresa ─────────────

# LinkedIn NO expone un selector de identidad en el comment box del feed.
# El mecanismo oficial para comentar como página de empresa es el parámetro
# ?actorCompanyId=<CompanyId> en la URL del post: al cargar con ese parámetro,
# la foto del comment box cambia al logo de la empresa y TODO lo que se escriba
# en esa página se publica como la empresa. Requiere ser admin de la página.
# Referencia del mecanismo: guías PowerIn / artículos LinkedIn (método URL).

def _company_numeric_id_from_url(url: str):
    """Extrae el Company ID numérico de una URL tipo /company/<id>/admin/..."""
    m = re.search(r"/company/(\d+)/", url or "")
    return m.group(1) if m else None


# Caché de Company IDs: el ID numérico de cada empresa no cambia durante la sesión.
_company_id_cache: dict = {}

async def _get_company_numeric_id(page, company_url: str):
    """Navega a la página de empresa y extrae el Company ID numérico del
    redirect al panel de admin (ej. /company/115807411/admin/dashboard/).
    Retorna el ID como str, o None si no se pudo (no admin / login requerido).
    Usa caché de sesión por URL de empresa."""
    global _company_id_cache
    cached = _company_id_cache.get(company_url)
    if cached:
        print(f"  [caché] Company ID de {company_url}: {cached}")
        return cached
    try:
        # Si el ID numérico ya viene en la URL de la empresa, no hace falta navegar
        direct_id = _company_numeric_id_from_url(company_url)
        if direct_id:
            _company_id_cache[company_url] = direct_id
            print(f"  Company ID directo de la URL: {direct_id}")
            return direct_id
        await page.goto(company_url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(5)
        company_id = _company_numeric_id_from_url(page.url)
        if company_id:
            _company_id_cache[company_url] = company_id
        return company_id
    except Exception as e:
        print(f"Error obteniendo company ID: {e}")
        return None


async def _resolve_canonical_permalink(page, permalink: str) -> str:
    """Resuelve un permalink de LinkedIn a su forma canónica (sin query params).
    Los enlaces 'Copiar enlace' devuelven short links lnkd.in que redirigen y
    PIERDEN el parámetro actorCompanyId, por eso se resuelven primero."""
    if not permalink:
        return ""
    if "lnkd.in" in permalink:
        try:
            await page.goto(permalink, timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(7)
            return page.url.split("?")[0]
        except Exception as e:
            print(f"Error resolviendo short link: {e}")
            return permalink.split("?")[0]
    return permalink.split("?")[0]


async def _get_post_permalink_clipboard(page, post_locator) -> str:
    """Obtiene el permalink real de un post del feed usando el método del
    portapapeles (menú de controles → 'Copiar enlace a la publicación').
    Es el método fiable cuando el extractor estático devuelve URLs vacías
    o links de páginas de empresa."""
    try:
        clicked = await post_locator.evaluate('''el => {
            const btn = el.querySelector('button[aria-label*="Abrir el menú de controles"], button[aria-label*="Open control menu"]');
            if (!btn) return 'NO_BTN';
            btn.scrollIntoView({behavior: 'instant', block: 'center'});
            btn.click();
            return 'CLICKED';
        }''')
        if clicked != 'CLICKED':
            return ""
        await asyncio.sleep(1.2)
        found = await page.evaluate('''() => {
            const items = [...document.querySelectorAll('[role="menuitem"]')];
            const it = items.find(i => /copiar enlace/i.test(i.innerText || ''));
            if (it) { it.click(); return true; }
            return false;
        }''')
        await asyncio.sleep(0.8)
        url = None
        if found:
            try:
                url = await page.evaluate("navigator.clipboard.readText().catch(() => null)")
            except Exception:
                url = None
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        return url or ""
    except Exception as e:
        print(f"Error obteniendo permalink via clipboard: {e}")
        return ""


async def _verify_company_identity(page, post_locator, company_name: str) -> bool:
    """Verifica que el comment box está comentando COMO la página de empresa.
    Método: comparar el avatar del composer (izquierda del textbox) con la foto
    del perfil personal. Si el composer muestra el logo de la empresa
    ('company-logo' en el src, o src distinto al avatar personal), OK."""
    try:
        composer_avatar = await post_locator.evaluate('''el => {
            const box = el.querySelector('div[role="textbox"], [contenteditable="true"], .tiptap');
            if (!box) return null;
            const tbRect = box.getBoundingClientRect();
            for (const im of document.querySelectorAll('img')) {
                const r = im.getBoundingClientRect();
                if (r.width < 10 || r.height < 10) continue;
                if (Math.abs(r.top - tbRect.top) < 80 && r.left < tbRect.left && tbRect.left - r.right < 160) {
                    return im.src || im.alt || '';
                }
            }
            return null;
        }''')
        if not composer_avatar:
            return False

        # 1) El src contiene 'company-logo' → logo de empresa confirmado
        if "company-logo" in composer_avatar:
            print(f"  ✓ Composer muestra logo de empresa (company-logo)")
            return True

        # 2) Comparar contra el avatar personal (menú Yo / me photo)
        personal = await page.evaluate('''() => {
            const roots = [document];
            const interop = document.querySelector('#interop-outlet');
            if (interop && interop.shadowRoot) roots.push(interop.shadowRoot);
            for (const root of roots) {
                const imgs = [...root.querySelectorAll('img')];
                const me = imgs.find(im => {
                    const r = im.getBoundingClientRect();
                    return r.width >= 24 && r.width <= 60 && r.height >= 24 && r.height <= 60 &&
                           (/profile-displayphoto|profile_original/i.test(im.src || '') || (im.alt && /foto de perfil|profile/i.test(im.alt)));
                });
                if (me) return me.src || '';
            }
            return '';
        }''')
        if personal and composer_avatar and composer_avatar != personal:
            print(f"  ✓ Composer avatar != avatar personal → identidad de empresa")
            return True

        return False
    except Exception as e:
        print(f"Error verificando identidad: {e}")
        return False


@mcp.tool()
async def company_comment_on_post(company_name: str, comment_text: str, author_name: str = "", post_url: str = "") -> str:
    """
    Comenta una publicación de LinkedIn USANDO LA IDENTIDAD DE UNA PÁGINA DE EMPRESA
    administrada por el perfil logueado (cualquier compañía, no una fija).

    Mecanismo: navega al post con el parámetro ?actorCompanyId=<CompanyId>, lo que
    activa el modo 'comentar como empresa' de LinkedIn (el comment box muestra el
    logo de la página y el comentario se publica a nombre de la empresa).

    Args:
        company_name: Nombre de la página de empresa gestionada desde la que comentar
                      (ej. "Aillu"). Match por substring contra las páginas gestionadas.
        comment_text: Texto del comentario.
        author_name: (Opcional) Autor del post a localizar escaneando el feed.
        post_url: (Opcional) Permalink del post (alternativa a author_name).
                  Debe indicarse author_name O post_url.

    REGLA MANDATORIA DE CALIDAD: Prohibido comentarios genéricos en lote o plantillas
    repetitivas (ej. 'Totalmente de acuerdo...', 'Excelente aporte...'). El comentario debe
    ser 100% único y analizar específicamente la tesis de la publicación del autor.
    """
    global CURRENT_ENGINE

    # ── Validación de entrada ──
    if not author_name and not post_url:
        return json.dumps({
            "error": "Debe indicarse 'author_name' (escaneo de feed) o 'post_url' (permalink directo).",
        }, indent=2, ensure_ascii=False)

    CURRENT_ENGINE = "chromium"
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
        try:
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
        try:
            page = await get_single_linkedin_page(context)
        except Exception as e:
            return f"Error: Could not get LinkedIn page: {e}"

        # ── 1. Resolver la compañía contra las páginas gestionadas ──
        managed = await _fetch_managed_companies(page)
        if "error" in managed:
            return json.dumps(managed, indent=2, ensure_ascii=False)

        companies = managed.get("companies", [])
        query = company_name.lower().strip()
        match = next((c for c in companies if c["name"].lower().strip() == query), None)
        if not match:
            match = next((c for c in companies if query in c["name"].lower()), None)
        if not match:
            return json.dumps({
                "error": f"No se encontró ninguna página gestionada que coincida con '{company_name}'.",
                "available_companies": [c["name"] for c in companies],
                "hint": "Usa get_linkedin_managed_pages() para ver las páginas disponibles.",
            }, indent=2, ensure_ascii=False)

        company_slug = _company_slug_from_url(match["url"])
        company_platform = f"linkedin_company:{company_slug}"
        print(f"Company resolved: '{match['name']}' ({match['url']}) -> platform='{company_platform}'")

        # ── 2. Obtener el Company ID numérico (necesario para actorCompanyId) ──
        company_id = await _get_company_numeric_id(page, match["url"])
        if not company_id:
            return json.dumps({
                "error": f"No se pudo obtener el Company ID de '{match['name']}'.",
                "hint": "Asegúrate de tener permisos de administrador en la página y sesión iniciada en LinkedIn.",
                "url_visitada": match["url"],
            }, indent=2, ensure_ascii=False)
        print(f"Company ID numérico: {company_id}")

        # ── 3. Obtener el permalink del post ──
        if post_url:
            permalink = await _resolve_canonical_permalink(page, post_url)
        else:
            # Escaneo de feed por autor
            if "linkedin.com/feed" not in page.url:
                await page.goto("https://www.linkedin.com/feed/")
                await page.wait_for_load_state("domcontentloaded")
                await asyncio.sleep(5)
            target_index, target_text, target_clean_author = await _find_post_in_feed(page, author_name)
            if target_index is None:
                return f"Error: Post by author '{author_name}' was not found in the feed after scrolling."
            post_locator_feed = page.locator(_POSTS_LOCATOR).nth(target_index)
            await post_locator_feed.evaluate("el => el.scrollIntoView({ behavior: 'instant', block: 'center' })")
            await asyncio.sleep(2)
            permalink = await _get_post_permalink_clipboard(page, post_locator_feed)
            if not permalink:
                permalink = await post_locator_feed.evaluate(_POST_URL_EXTRACT_JS)

        if not permalink:
            return "Error: No se pudo obtener el enlace del post (permalink vacío)."

        canonical = await _resolve_canonical_permalink(page, permalink)
        if not canonical:
            return "Error: No se pudo resolver el enlace del post."

        # ── 4. Navegar al post con identidad de empresa ──
        comment_url = f"{canonical}?actorCompanyId={company_id}"
        print(f"Navigating to post with company identity: {comment_url}")
        await page.goto(comment_url, timeout=45000, wait_until="domcontentloaded")
        await asyncio.sleep(8)

        # ── 5. Localizar el post en el permalink ──
        target_index, target_text, target_clean_author = None, None, None
        posts_locator = page.locator(_POSTS_LOCATOR)
        posts_count = await posts_locator.count()
        print(f"Found {posts_count} post containers on permalink page.")
        for i in range(posts_count):
            item = posts_locator.nth(i)
            text = await item.inner_text()
            if len(text.strip()) > 80:
                target_index = i
                target_text = text
                author = await item.evaluate(_AUTHOR_EXTRACT_JS)
                target_clean_author = _clean_author(author)
                break
        if target_index is None:
            return f"Error: No se encontró el post en {canonical}."

        # ── 6. Dedup DB independiente por identidad ──
        content_hash = hashlib.sha256(target_text.encode("utf-8")).hexdigest()
        if check_already_commented(target_clean_author, content_hash, platform=company_platform):
            return f"Warning: La página '{match['name']}' ya comentó este post (hash '{content_hash[:8]}'). Skip."

        post_locator = page.locator(_POSTS_LOCATOR).nth(target_index)
        await post_locator.evaluate("el => el.scrollIntoView({ behavior: 'instant', block: 'center' })")
        await asyncio.sleep(2)

        # ── 7. Abrir caja de comentario y chequear duplicado en DOM ──
        comment_box = await _open_comment_box(page, post_locator)
        already_in_dom = await post_locator.evaluate(_DOM_OWN_COMMENT_JS)
        if already_in_dom:
            print("  ✓ Found existing comment by active user in DOM. Aborting.")
            return "Warning: Ya existe un comentario del usuario activo en este post. Abortado."

        if comment_box is None:
            return "Error: Could not open the comment editor for this post."

        # ── 8. Verificar que la identidad activa es la empresa (guard) ──
        verified = await _verify_company_identity(page, post_locator, match["name"])
        if not verified:
            return json.dumps({
                "error": "No se pudo confirmar que la identidad del comentario sea la página de empresa.",
                "reason": "El avatar del comment box no coincide con el logo de la empresa. "
                          "Posibles causas: la URL con actorCompanyId no aplicó, o no eres admin de la página.",
                "hint": f"Revisa manualmente {comment_url} para verificar el estado del comment box.",
            }, indent=2, ensure_ascii=False)

        # ── 9. Escribir, enviar y verificar ──
        success = await _type_and_submit_comment(page, post_locator, comment_text, comment_box)
        if not success:
            return "Error: Comment box did not clear after multiple submission attempts."

        record_comment(target_clean_author, content_hash, comment_text, canonical, platform=company_platform)
        return (f"✓ Comment posted AS '{match['name']}' ({company_platform}) on "
                f"'{target_clean_author or author_name or canonical}' post.\nPost URL: {canonical}")

async def run_read_whatsapp_contact_chat(context, contact: str, limit: int = 15):
    page = await get_single_whatsapp_page(context)

    # Wait for chat list
    try:
        await page.wait_for_selector('#pane-side, div[aria-label="Lista de chats"]', timeout=15000)
    except Exception:
        qr_bytes = await _check_qr_and_capture(page)
        if qr_bytes:
            return qr_bytes
        return "Error: Could not find chat list. WhatsApp might still be loading."

    try:
        # ── 1. GO BACK TO MAIN CHAT LIST ──
        # Press Escape to close any open conversation/panel first
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        # ── 2. SEARCH CONTACT ──
        search_box = page.locator('[role="textbox"]').first
        await search_box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.3)
        await search_box.fill(contact)
        await asyncio.sleep(2)

        # ── 3. CLICK FIRST RESULT + ENTER ──
        clicked = await page.evaluate("""(contactName) => {
            const rows = document.querySelectorAll('#pane-side div[role="row"]');
            for (const row of rows) {
                const text = row.innerText.toLowerCase();
                if (text.includes(contactName.toLowerCase())) {
                    const cell = row.querySelector('[role="gridcell"]');
                    if (cell) {
                        cell.click();
                        return true;
                    }
                    row.click();
                    return true;
                }
            }
            if (rows.length > 0) {
                const cell = rows[0].querySelector('[role="gridcell"]');
                if (cell) cell.click();
                else rows[0].click();
                return 'fallback';
            }
            return false;
        }""", contact)
        if not clicked:
            await search_box.press("Enter")
        await asyncio.sleep(1)

        # Try pressing Enter again to open the conversation (WhatsApp Business)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2)

        # ── 4. VERIFY WITH SPIKE ──
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

        # ── 5. EXTRACT MESSAGES ──
        await asyncio.sleep(1)

        messages_data = await page.evaluate("""(limit) => {
            const panel = document.querySelector('[data-testid="conversation-panel-messages"]');
            if (!panel) return [];

            const results = [];

            // ── Método 1: data-pre-plain-text ──────────────────────────────────────
            // Atributo confiable: '[HH:MM, DD/MM/YYYY] Nombre:'
            // Presente en todos los mensajes de texto, no depende de clases CSS.
            const copyableTexts = panel.querySelectorAll('[data-pre-plain-text]');
            if (copyableTexts.length > 0) {
                // Obtener el nombre del contacto del header (quien NO somos nosotros)
                const headerSpan = document.querySelector('#main header span[dir="auto"]');
                const contactName = headerSpan ? headerSpan.innerText.trim().toLowerCase() : null;

                copyableTexts.forEach(el => {
                    const prePlain = el.getAttribute('data-pre-plain-text') || '';
                    // Formato: '[HH:MM, DD/MM/YYYY] Nombre:'  o  '[HH:MM] Nombre:'
                    const senderMatch = prePlain.match(/]\\s*(.+?):\\s*$/);

                    const senderRaw = senderMatch ? senderMatch[1].trim() : null;

                    // Buscar el texto del mensaje en el nodo hermano o hijo
                    const textEl = el.querySelector('.copyable-text, span[class*="selectable-text"]') || el;
                    const text = textEl.innerText.trim();
                    if (!text || text.length < 1) return;

                    let isOutgoing = false;
                    if (senderRaw && contactName) {
                        // Si el remitente NO es el contacto → es outgoing (lo enviamos nosotros)
                        isOutgoing = !senderRaw.toLowerCase().includes(contactName);
                    }

                    results.push({ text, isOutgoing, sender: senderRaw });
                });
            }

            // ── Método 2: Fallback por clases CSS legacy ───────────────────────────
            // Solo si data-pre-plain-text no funcionó (grupos sin ese atributo, etc.)
            if (results.length === 0) {
                const containers = panel.querySelectorAll('.msg-container, [data-testid^="conv-msg-"]');
                containers.forEach(el => {
                    const text = el.innerText.trim();
                    if (!text || text.length < 2) return;
                    const html = el.outerHTML;
                    const isOutgoing = html.includes('tail-out') && !html.includes('tail-in');
                    results.push({ text, isOutgoing, sender: null });
                });
            }

            // ── Método 3: Fallback geométrico ─────────────────────────────────────
            // Si no hay clases ni atributos: mensajes que empiezan en >40% del ancho
            // son outgoing (burbuja derecha), el resto son incoming (burbuja izquierda).
            if (results.length === 0) {
                const allMsgs = panel.querySelectorAll('[role="row"]');
                const panelWidth = panel.getBoundingClientRect().width || window.innerWidth;
                allMsgs.forEach(el => {
                    const text = el.innerText.trim();
                    if (!text || text.length < 2) return;
                    const rect = el.getBoundingClientRect();
                    const isOutgoing = rect.left > panelWidth * 0.40;
                    results.push({ text, isOutgoing, sender: null });
                });
            }

            // Deduplicar y limitar
            const seen = new Set();
            const unique = results.filter(m => {
                if (seen.has(m.text)) return false;
                seen.add(m.text);
                return true;
            });

            return unique.slice(-limit);
        }""", limit)

        if not messages_data:
            html_preview = await page.evaluate("document.body.innerText.slice(0, 500)")
            return f"No messages found in chat with {actual_chat_title}. Whatsapp Business preview: {html_preview}"

        messages_list = messages_data

        # Format the output
        result_lines = [f"--- Chat history with {actual_chat_title} ---"]
        for msg in messages_list:
            sender = "You" if msg['isOutgoing'] else actual_chat_title
            clean_text = " | ".join(
                line.strip() for line in msg['text'].split('\n') if line.strip()
            )
            result_lines.append(f"{sender}: {clean_text}")

        chat_text = "\n".join(result_lines)
        save_page_context(page.url, f"WhatsApp Chat History with {actual_chat_title}", chat_text)
        return chat_text

    except Exception as e:
        return f"Error reading chat: {e}"


# ─── LinkedIn: Mensajería / Chat de Contactos ──────────────

@mcp.tool()
async def list_linkedin_conversations() -> str:
    """
    Lista las conversaciones recientes del chat de LinkedIn del perfil logueado.
    Devuelve un JSON con el nombre del contacto, fragmento del último mensaje,
    estado de no leído, el enlace de su perfil (si está disponible) y el enlace de la conversación.

    Requiere sesión activa en LinkedIn (motor 'chromium').
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()
    
    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                return f"Error: Could not connect to browser on {cdp_url}. Details: {e}"

            contexts = browser.contexts
            if not contexts:
                return "Error: No browser contexts found."
            context = contexts[0]

            try:
                page = await get_single_linkedin_page(context, "https://www.linkedin.com/messaging/")
            except Exception as e:
                return f"Error: Could not get LinkedIn page: {e}"

            import sys
            import os
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            import linkedin_chats
            result = await linkedin_chats.list_conversations(page)
            # Guardar en Atlántico
            save_page_context(page.url, "LinkedIn Recent Conversations", result, source="linkedin")
            return result

    except Exception as e:
        return f"Error inesperado al listar conversaciones: {e}"


@mcp.tool()
async def read_linkedin_messages(contact_name: str = "", limit: int = 20) -> str:
    """
    Lee los mensajes de un chat de LinkedIn. Si se proporciona contact_name, busca
    ese contacto en la lista de chats recientes y hace clic en él antes de leer.
    Si no se proporciona contact_name, lee el chat que esté actualmente abierto en pantalla.

    Args:
        contact_name: Nombre del contacto a leer (ej. "John Doe"). Opcional.
        limit: Número máximo de mensajes a recuperar.
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()

    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                return f"Error: Could not connect to browser on {cdp_url}. Details: {e}"

            contexts = browser.contexts
            if not contexts:
                return "Error: No browser contexts found."
            context = contexts[0]

            try:
                page = await get_single_linkedin_page(context, "https://www.linkedin.com/messaging/")
            except Exception as e:
                return f"Error: Could not get LinkedIn page: {e}"

            import sys
            import os
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            import linkedin_chats
            result = await linkedin_chats.read_messages(page, contact_name, limit)
            
            # Guardar en Atlántico con formato de texto similar a WhatsApp
            try:
                messages_list = json.loads(result)
                actual_chat_title = contact_name if contact_name else "Active Chat"
                result_lines = [f"--- LinkedIn Chat history with {actual_chat_title} ---"]
                for msg in messages_list:
                    sender = msg.get('sender', 'Unknown')
                    clean_text = " | ".join(
                        line.strip() for line in msg.get('text', '').split('\n') if line.strip()
                    )
                    result_lines.append(f"{sender}: {clean_text}")
                chat_text = "\n".join(result_lines)
                save_page_context(page.url, f"LinkedIn Chat History with {actual_chat_title}", chat_text, source="linkedin")
            except Exception as ex:
                print(f"Warning: Could not save LinkedIn messages to Atlantico: {ex}")
                
            return result

    except Exception as e:
        return f"Error inesperado al leer mensajes: {e}"


@mcp.tool()
async def send_linkedin_message(contact_name_or_url: str, message: str) -> str:
    """
    Envía un mensaje de texto a un contacto en LinkedIn.
    Soporta buscar al contacto por su nombre o ir directamente a su URL de perfil.

    Args:
        contact_name_or_url: Nombre del contacto (ej. "John Doe") o URL de su perfil (ej. "https://www.linkedin.com/in/nombre-de-usuario/").
        message: El mensaje de texto a enviar.
    """
    global CURRENT_ENGINE
    CURRENT_ENGINE = "chromium"
    cdp_url = get_cdp_url()
    ensure_obscura()

    is_profile_url = contact_name_or_url.startswith("http") and "linkedin.com/in/" in contact_name_or_url

    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                return f"Error: Could not connect to browser on {cdp_url}. Details: {e}"

            contexts = browser.contexts
            if not contexts:
                return "Error: No browser contexts found."
            context = contexts[0]

            import sys
            import os
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))
            import linkedin_chats
            if is_profile_url:
                try:
                    page = await get_single_linkedin_page(context, contact_name_or_url)
                except Exception as e:
                    return f"Error: Could not open profile URL: {e}"
                result = await linkedin_chats.send_message_via_profile(page, contact_name_or_url, message, human_type)
                # Guardar en Atlántico si fue exitoso
                if result.startswith("✓"):
                    save_page_context(page.url, f"LinkedIn Sent Message to {contact_name_or_url}", f"Sent message: {message}", source="linkedin")
                return result
            else:
                try:
                    page = await get_single_linkedin_page(context, "https://www.linkedin.com/messaging/")
                except Exception as e:
                    return f"Error: Could not get LinkedIn messaging page: {e}"
                result = await linkedin_chats.send_message_via_messaging(page, contact_name_or_url, message, human_type)
                # Guardar en Atlántico si fue exitoso
                if result.startswith("✓"):
                    save_page_context(page.url, f"LinkedIn Sent Message to {contact_name_or_url}", f"Sent message: {message}", source="linkedin")
                return result

    except Exception as e:
        return f"Error inesperado al enviar mensaje: {e}"


# ─── Retina Semántica: Tools MCP ─────────────────────────────

@mcp.tool()
async def get_accessibility_tree(engine: str = "chromium", interesting_only: bool = True) -> str:
    """
    Returns the accessibility tree of the current web page as a compact text map.
    Shows the semantic structure: headings, buttons, inputs, links, with their
    states (focused, disabled, checked). Useful to understand the page layout
    and find elements before interacting. Output is optimized for low token usage.

    Args:
        engine: 'chromium' (default) or 'obscura'
        interesting_only: If True, only shows interactive/meaningful nodes
    """
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            url = page.url
            title = await page.title()
            # Check cache
            cached = _retina_cache_get(url, "a11y_tree", ttl=180)
            if cached:
                return cached
            # Build tree
            snapshot = await page.accessibility.snapshot(interesting_only=interesting_only)
            if not snapshot:
                return f"⚠ No accessibility tree available for '{title}' ({url}). The page may still be loading."
            tree_text = _build_a11y_node(snapshot)
            # Truncate to ~2000 chars to keep token usage low
            if len(tree_text) > 2000:
                tree_text = tree_text[:2000] + "\n... (truncado, usar interesting_only=True para reducir)"
            result = f"A11y Tree — {title}\n{url}\n{'─' * 40}\n{tree_text}"
            # Cache
            content_hash = _get_page_content_hash(url, title)
            _retina_cache_set(url, content_hash, "a11y_tree", result)
            return result
        except Exception as e:
            return f"Error building accessibility tree: {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def get_interactive_map(engine: str = "chromium") -> str:
    """
    Scans the current web page and returns a numbered list of ALL interactive
    elements visible on screen (buttons, inputs, links, selects, checkboxes).
    Each element gets an ID like [1], [2], [3] that you can use with
    page_click_by_id() or page_fill_by_id() to interact without CSS selectors.

    This is the primary tool for blind/text-only models to "see" a web page.
    Call this first to understand what's on screen, then use the IDs to act.

    Args:
        engine: 'chromium' (default) or 'obscura'
    """
    global _retina_element_map, _retina_map_url, _retina_map_ts
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            url = page.url
            title = await page.title()
            # Check cache — also restore in-memory map if still valid
            cached = _retina_cache_get(url, "interactive_map", ttl=120)
            if cached and _retina_map_url == url and (time.time() - _retina_map_ts) < 120:
                return cached
            # Execute JS extractor
            elements = await page.evaluate(_INTERACTIVE_MAP_JS)
            if not elements:
                return f"No interactive elements found on '{title}' ({url}). The page may be empty or still loading."
            # Build in-memory map and text output
            _retina_element_map = {}
            lines = []
            for i, el in enumerate(elements, 1):
                _retina_element_map[i] = el
                tag = el.get("tag", "?")
                role = el.get("role", "")
                name = el.get("name", "")
                label = role.capitalize() if role else tag.capitalize()
                if tag == "input":
                    input_type = el.get("type", "text")
                    label = f"Input[{input_type}]"
                coords = f"({el.get('x', 0)}, {el.get('y', 0)})"
                parts = [f"[{i}]", f"{label:16s}", f'"{name}"' if name else '""', coords]
                # Show current value for inputs
                if "value" in el and el["value"]:
                    parts.append(f'→ "{el["value"]}"')
                if el.get("checked") is not None:
                    parts.append("☑" if el["checked"] else "☐")
                if el.get("href"):
                    parts.append(f'→ {el["href"]}')
                lines.append("  ".join(parts))
            _retina_map_url = url
            _retina_map_ts = time.time()
            header = f"Interactive Map — {title}\n{url}\n{len(elements)} elements found\n{'─' * 50}"
            result = header + "\n" + "\n".join(lines)
            # Cache
            content_hash = _get_page_content_hash(url, title)
            _retina_cache_set(url, content_hash, "interactive_map", result)
            return result
        except Exception as e:
            return f"Error building interactive map: {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_click_by_id(element_id: int, engine: str = "chromium") -> str:
    """
    Click on an element using its numeric ID from get_interactive_map().
    Much more reliable than CSS selectors on dynamic sites.

    IMPORTANT: Call get_interactive_map() first to get the list of IDs.
    After clicking, the page may change — call get_interactive_map() again
    to see the updated state.

    Args:
        element_id: The numeric ID shown in brackets [1], [2], etc.
        engine: 'chromium' (default) or 'obscura'
    """
    global _retina_element_map
    if not _retina_element_map:
        return "⚠ No interactive map loaded. Call get_interactive_map() first to scan the page."
    if element_id not in _retina_element_map:
        available = sorted(_retina_element_map.keys())
        return f"⚠ Element [{element_id}] not found. Available IDs: {available[:20]}{'...' if len(available) > 20 else ''}"
    el = _retina_element_map[element_id]
    name = el.get("name", "")
    tag = el.get("tag", "?")
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            # Primary: click by coordinates (center of bounding box)
            cx = el.get("x", 0) + el.get("w", 0) // 2
            cy = el.get("y", 0) + el.get("h", 0) // 2
            try:
                await page.mouse.click(cx, cy)
                _retina_invalidate()  # Page likely changed
                return f'✓ Click en [{element_id}] {tag} "{name}" ({cx}, {cy})'
            except Exception:
                pass
            # Fallback: click by selector
            selector = el.get("selector", "")
            if selector:
                try:
                    await page.click(selector, timeout=3000)
                    _retina_invalidate()
                    return f'✓ Click (selector) en [{element_id}] {tag} "{name}"'
                except Exception as e2:
                    return f'Error: click failed on [{element_id}] {tag} "{name}" — coords ({cx},{cy}) and selector "{selector}" both failed: {e2}'
            return f'Error: click by coordinates failed on [{element_id}] and no fallback selector available.'
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_fill_by_id(element_id: int, text: str, engine: str = "chromium") -> str:
    """
    Type text into an input field using its numeric ID from get_interactive_map().
    Clears existing content before typing. Works with input, textarea, and
    contenteditable elements.

    IMPORTANT: Call get_interactive_map() first to get the list of IDs.

    Args:
        element_id: The numeric ID of an input/textarea from the interactive map
        text: The text to type into the field
        engine: 'chromium' (default) or 'obscura'
    """
    global _retina_element_map
    if not _retina_element_map:
        return "⚠ No interactive map loaded. Call get_interactive_map() first to scan the page."
    if element_id not in _retina_element_map:
        available = sorted(_retina_element_map.keys())
        return f"⚠ Element [{element_id}] not found. Available IDs: {available[:20]}{'...' if len(available) > 20 else ''}"
    el = _retina_element_map[element_id]
    tag = el.get("tag", "?")
    name = el.get("name", "")
    fillable = {"input", "textarea", "select"}
    is_contenteditable = el.get("role") == "textbox" or tag in fillable
    if not is_contenteditable:
        return f"⚠ Element [{element_id}] is a {tag}, not a fillable field. Use page_click_by_id() instead."
    selector = el.get("selector", "")
    if not selector:
        return f"⚠ No selector available for [{element_id}]. Try page_click_by_id() followed by keyboard input."
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            await page.wait_for_selector(selector, timeout=3000)
            await page.fill(selector, text)
            preview = text[:50].replace('\n', '\\n')
            suffix = '...' if len(text) > 50 else ''
            _retina_invalidate()  # Field value changed
            return f"✓ Escrito '{preview}{suffix}' en [{element_id}] {tag} \"{name}\""
        except Exception as e:
            return f'Error al llenar [{element_id}] {tag} "{name}" con selector "{selector}": {e}'
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


# ─── Helper: página activa ──────────────────────────────────

async def _get_active_page(engine="chromium"):
    """Conecta al motor indicado, lo asegura corriendo, y devuelve (playwright, browser, page)."""
    global CURRENT_ENGINE
    if engine:
        CURRENT_ENGINE = engine
    cdp_url = get_cdp_url()
    ensure_obscura()
    p = await async_playwright().start()
    try:
        browser = await p.chromium.connect_over_cdp(cdp_url)
    except Exception as e:
        await p.stop()
        raise ConnectionError(f"No se pudo conectar a {cdp_url}: {e}")
    contexts = browser.contexts
    if not contexts:
        await p.stop()
        raise RuntimeError("No hay contextos de navegador.")
    context = contexts[0]
    pages = context.pages
    if not pages:
        await p.stop()
        raise RuntimeError("No hay páginas abiertas en el navegador.")
    page = pages[-1]
    for pt in pages:
        if pt.url not in ("about:blank", ""):
            page = pt
            break
    return p, browser, page

# ─── Tools genéricas de navegación ──────────────────────────

@mcp.tool()
async def page_fill(selector: str, text: str, engine: str = "chromium") -> str:
    """
    Escribe texto en un campo de formulario identificado por selector CSS.
    Borra el contenido existente antes de escribir.

    Args:
        selector: Selector CSS del campo (ej. '#titulo', 'input[name="precio"]')
        text: Texto a escribir
        engine: 'chromium' (default) o 'obscura'
    """
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            await page.wait_for_selector(selector, timeout=5000)
            await page.fill(selector, text)
            preview = text[:60].replace('\n', '\\n')
            suffix = '...' if len(text) > 60 else ''
            return f"✓ Escrito '{preview}{suffix}' en '{selector}'."
        except Exception as e:
            return f"Error al llenar '{selector}': {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_click(selector: str, engine: str = "chromium") -> str:
    """
    Hace click en un elemento de la página identificado por selector CSS.
    Espera hasta 5s a que el elemento aparezca.

    Args:
        selector: Selector CSS del elemento a clickear
        engine: 'chromium' (default) o 'obscura'
    """
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            await page.wait_for_selector(selector, timeout=5000)
            await page.click(selector)
            return f"✓ Click ejecutado en '{selector}'."
        except Exception as e:
            return f"Error al hacer click en '{selector}': {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_evaluate(js_code: str, engine: str = "chromium") -> str:
    """
    Ejecuta código JavaScript en la página activa y devuelve el resultado.

    Args:
        js_code: Código JavaScript (ej. 'document.title' o '(function(){ return 1+1; })()')
        engine: 'chromium' (default) o 'obscura'
    """
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            result = await page.evaluate(js_code)
            result_str = str(result) if result is not None else "undefined"
            if len(result_str) > 3000:
                result_str = result_str[:3000] + f"\n... (truncado, {len(result_str)} chars totales)"
            return f"✓ Resultado:\n{result_str}"
        except Exception as e:
            return f"Error al evaluar JS: {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_screenshot(engine: str = "chromium") -> str:
    """
    Toma una captura de pantalla de la página activa y la devuelve como imagen PNG.
    Útil para verificar el estado del navegador o depurar selectores.

    Args:
        engine: 'chromium' (default) o 'obscura'
    """
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            screenshot_bytes = await page.screenshot(type="png", full_page=False)
            from mcp.types import TextContent as _TextContent
            return [
                _TextContent(type="text", text=f"📸 Screenshot de: {page.url}"),
                MCPImage(data=screenshot_bytes, format="png")
            ]
        except Exception as e:
            return f"Error al tomar screenshot: {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_get_text(selector: str, engine: str = "chromium") -> str:
    """
    Obtiene el texto visible de un elemento identificado por selector CSS.

    Args:
        selector: Selector CSS del elemento
        engine: 'chromium' (default) o 'obscura'
    """
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            await page.wait_for_selector(selector, timeout=5000)
            text = await page.text_content(selector)
            return text.strip() if text else "(elemento vacío)"
        except Exception as e:
            return f"Error al obtener texto de '{selector}': {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_upload(selector: str, file_path: str, engine: str = "chromium") -> str:
    """
    Sube un archivo (ej. foto) a un input de tipo file.
    La ruta debe ser absoluta en el sistema de archivos del host.

    Args:
        selector: Selector CSS del input[type=file]
        file_path: Ruta absoluta al archivo (ej. /home/alnar/foto.jpg)
        engine: 'chromium' (default) o 'obscura'
    """
    import os
    if not os.path.isfile(file_path):
        return f"Error: El archivo '{file_path}' no existe."
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            await page.wait_for_selector(selector, timeout=5000)
            await page.set_input_files(selector, file_path)
            return f"✓ Archivo '{os.path.basename(file_path)}' subido a '{selector}'."
        except Exception as e:
            return f"Error al subir archivo a '{selector}': {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


@mcp.tool()
async def page_select_option(selector: str, value: str, engine: str = "chromium") -> str:
    """
    Selecciona una opción en un elemento <select> por su value (o texto si no hay value).

    Args:
        selector: Selector CSS del elemento <select>
        value: Valor (atributo value) de la opción a seleccionar
        engine: 'chromium' (default) o 'obscura'
    """
    try:
        p, browser, page = await _get_active_page(engine)
        try:
            await page.wait_for_selector(selector, timeout=5000)
            await page.select_option(selector, value)
            return f"✓ Opción '{value}' seleccionada en '{selector}'."
        except Exception as e:
            return f"Error al seleccionar opción en '{selector}': {e}"
        finally:
            await p.stop()
    except Exception as e:
        return f"Error de conexión: {e}"


# ─── ClickUp API Tool ───────────────────────────────────────

@mcp.tool()
async def clickup_api(api_key: str, method: str = "GET", path: str = "", data: str = "") -> str:
    """
    Llama cualquier endpoint de ClickUp REST API.
    api_key: tu token pk_...
    method: GET, POST, PUT, DELETE
    path: ruta después de /api/v2 (ej: /team/{id}/space, /list/{id}/task)
    data: JSON string opcional para POST/PUT (ej: '{"name":"test"}')
    
    Ejemplos:
      clickup_api(api_key, "GET", "/team")
      clickup_api(api_key, "GET", "/team/90132707763/space")
      clickup_api(api_key, "GET", "/space/901313864337/folder")
      clickup_api(api_key, "GET", "/list/1000500000001319/task")
      clickup_api(api_key, "PUT", "/list/1000500000001319", '{"override_statuses":true,"statuses":[{"status":"Nuevo","type":"open"}]}')
      clickup_api(api_key, "POST", "/list/1000500000001319/task", '{"name":"Mi tarea","priority":2}')
      clickup_api(api_key, "DELETE", "/task/wdyc7hjk30")
    """
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    import clickup as cu
    parsed_data = json.loads(data) if data.strip() else None
    result = cu.api(api_key, method, path, parsed_data)
    return cu.pretty(result)

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
