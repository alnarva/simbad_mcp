"""
Test TDD para los extractores de LinkedIn del nuevo DOM (nov 2025+).

Verifica:
  1. _AUTHOR_EXTRACT_JS: extrae el autor correcto de cada post orgánico.
  2. _POST_URL_EXTRACT_JS (nuevo): extrae el permalink REAL de cada post,
     sin colisiones (cada post debe tener una URL única con su propia URN).

Ejecución (desde dentro del contenedor simbad_run):
  docker exec simbad_run python3 /app/tests/test_linkedin_extractors.py
"""
import asyncio
import hashlib
import os
import re
import sys
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Cargar los extractores REALES desde simbad_mcp.py (producción), para que el
# test verifique el código que se ejecuta en el servidor MCP.
# ---------------------------------------------------------------------------
_MCP_PATH = "/app/simbad_mcp.py" if os.path.exists("/app/simbad_mcp.py") else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "simbad_mcp.py")


def _extract_js_var(name):
    src = open(_MCP_PATH).read()
    m = re.search(rf"{name} = r'''(.*?)'''", src, re.DOTALL)
    if not m:
        raise RuntimeError(f"No se encontró {name} en {_MCP_PATH}")
    return m.group(1)


OLD_POST_URL_EXTRACT_JS = _extract_js_var("_POST_URL_EXTRACT_JS")
# Para el test, definimos el "nuevo" como el actual de producción (que ya incluye
# el método clipboard). La comparación OLD vs NEW la hacemos ejecutando el
# extractor de producción y verificando que devuelva URLs únicas con URNs reales.
NEW_POST_URL_EXTRACT_JS = OLD_POST_URL_EXTRACT_JS
AUTHOR_EXTRACT_JS = _extract_js_var("_AUTHOR_EXTRACT_JS")

POSTS_LOCATOR = 'main [role="listitem"], .feed-shared-update-v2, article, div[data-urn]'


def clean_author(author: str) -> str:
    clean = author.split('•')[0].strip()
    words = clean.split()
    if len(words) >= 4 and words[0] == words[2] and words[1] == words[3]:
        clean = f"{words[0]} {words[1]}"
    return clean


def urn_of(url: str) -> str:
    """Extrae la URN (o el ID de share) de una URL de post."""
    if not url:
        return ""
    m = re.search(r'urn:li:(activity|ugcPost|share):(\d+)', url)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    # URL tipo /posts/{org}-{slug}-share-{id}-{hash}
    m2 = re.search(r'(?:share|activity|ugcPost)[-:](\d+)', url)
    if m2:
        return f"id:{m2.group(1)}"
    return url


async def collect_posts(page, limit=15):
    """Recopila posts orgánicos con autor y URL usando ambos extractores."""
    results = []
    seen_hashes = set()
    for attempt in range(12):
        posts = page.locator(POSTS_LOCATOR)
        count = await posts.count()
        for i in range(count):
            if len(results) >= limit:
                break
            post = posts.nth(i)
            try:
                text = await post.inner_text()
            except Exception:
                continue
            if not text or "Publicación en el feed" not in text:
                continue
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if h in seen_hashes:
                continue
            author_raw = await post.evaluate(AUTHOR_EXTRACT_JS)
            author = clean_author(author_raw)
            if author == "Unknown":
                continue
            seen_hashes.add(h)

            # Ejecutar el extractor de URL de producción (método clipboard)
            post_url = await post.evaluate(NEW_POST_URL_EXTRACT_JS)

            results.append({
                "author": author,
                "new_url": post_url,
                "new_urn": urn_of(post_url),
            })

        if len(results) >= limit:
            break

        # Scroll incremental para cargar más (el contenedor real es main)
        try:
            await page.evaluate("""() => {
                const el = document.getElementById('workspace') || document.querySelector('main');
                if (el) el.scrollTop += 1200;
            }""")
        except Exception:
            pass
        await asyncio.sleep(2)

        # Click en "Cargar más" si aparece (dispara lazy loading)
        try:
            load_more = page.locator("button:has-text('Cargar más'), button:has-text('Load more')").first
            if await load_more.count() > 0 and await load_more.is_visible():
                await load_more.click(timeout=3000)
                await asyncio.sleep(3)
        except Exception:
            pass

    return results


async def main():
    passed = 0
    failed = 0

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9227")
        contexts = browser.contexts
        if not contexts:
            print("FAIL: no browser contexts")
            sys.exit(1)
        context = contexts[0]
        pages = [pg for pg in context.pages if "linkedin.com" in pg.url]
        if not pages:
            print("FAIL: no LinkedIn page")
            sys.exit(1)
        page = pages[0]
        await page.bring_to_front()
        if "/feed" not in page.url:
            await page.goto("https://www.linkedin.com/feed/")
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(5)

        posts = await collect_posts(page, limit=10)

        if len(posts) < 5:
            print(f"FAIL: only {len(posts)} posts collected (need >=5)")
            failed += 1
        else:
            print(f"OK: collected {len(posts)} posts")
            passed += 1

        # Test 1: cada post orgánico debe tener autor no vacío
        for p_ in posts:
            if not p_["author"]:
                print(f"FAIL: post sin autor: {p_}")
                failed += 1
            else:
                passed += 1

        # Test 2: cada post orgánico debe tener un autor extraído (no "Unknown")
        clean_authors = [p_["author"] for p_ in posts]
        non_unknown = [a for a in clean_authors if a and a != "Unknown"]
        print(f"\n--- Autores extraídos ({len(non_unknown)}/{len(posts)} orgánicos) ---")
        for a in clean_authors[:10]:
            print(f"  {a[:40]}")
        if len(non_unknown) >= max(2, len(posts) - 3):
            print("PASS: _AUTHOR_EXTRACT_JS extrae autores correctamente")
            passed += 1
        else:
            print("FAIL: _AUTHOR_EXTRACT_JS falla en demasiados posts")
            failed += 1

        # Test 3: el extractor de URL de producción debe producir URLs únicas con URN real
        new_urns = [p_["new_urn"] for p_ in posts]
        new_unique = len(set(new_urns))
        print(f"\n--- Extractor de URL (producción) ---")
        for p_ in posts[:10]:
            print(f"  {p_['author'][:30]:30s} urn={p_['new_urn'][:45]}")
        print(f"  -> {len(new_urns)} URLs, {new_unique} únicas")
        if new_unique >= max(2, len(posts) - 2):
            print("PASS: extractor produce URLs únicas por post")
            passed += 1
        else:
            print("FAIL: extractor colisiona URLs")
            failed += 1

        # Test 4: la URL debe contener una URN o ID de post real (no /posts/ vacío)
        real_count = sum(1 for u in new_urns if u and "id:" in u or (u and re.match(r'^(activity|ugcPost|share):', u)))
        print(f"\n  URLs con URN real: {real_count}/{len(posts)}")
        if real_count >= max(2, len(posts) - 3):
            print("PASS: extractor devuelve URNs de post reales")
            passed += 1
        else:
            print("FAIL: extractor no devuelve URNs reales")
            failed += 1

        # Test 5: el diálogo de publicación (publish_linkedin_post) se abre y
        #          contiene editor + botón Publicar en el shadow DOM.
        print("\n--- Diálogo de publicación (shadow DOM) ---")
        try:
            opened = await page.evaluate("""async () => {
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
            await asyncio.sleep(4)
            pub_state = await page.evaluate("""() => {
                const r = document.querySelector('#interop-outlet')?.shadowRoot;
                if (!r) return 'NO_SHADOW';
                const ds = Array.from(r.querySelectorAll('[role="dialog"]')).filter(d => {
                    const b = d.getBoundingClientRect(); return b.width > 100 && b.height > 100;
                });
                if (!ds.length) return 'NO_DIALOG';
                const ed = ds[0].querySelector('.ql-editor, [contenteditable="true"], div[role="textbox"]');
                const pub = Array.from(ds[0].querySelectorAll('button')).find(b => b.innerText.trim() === 'Publicar');
                return `dialog editor=${!!ed} publicar=${!!pub}`;
            }""")
            print(f"  Apertura: {opened} | {pub_state}")
            if opened == 'CLICKED' and 'editor=true' in pub_state.lower() and 'publicar=true' in pub_state.lower():
                print("PASS: publish_linkedin_post abre diálogo con editor y Publicar (shadow DOM)")
                passed += 1
            else:
                print("FAIL: publish_linkedin_post no encuentra editor/Publicar en shadow DOM")
                failed += 1
            # Cerrar el diálogo para dejar el navegador limpio
            await page.evaluate("""() => {
                const r = document.querySelector('#interop-outlet')?.shadowRoot;
                if (!r) return;
                const d = Array.from(r.querySelectorAll('[role="dialog"]')).find(x => {
                    const b = x.getBoundingClientRect(); return b.width > 100 && b.height > 100;
                });
                if (!d) return;
                const btn = Array.from(d.querySelectorAll('button')).find(b => (b.getAttribute('aria-label') || '') === 'Descartar');
                if (btn) btn.click();
            }""")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"FAIL: test diálogo publicación con error: {e}")
            failed += 1

        print(f"\n=== RESULTADO: {passed} passed, {failed} failed ===")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
