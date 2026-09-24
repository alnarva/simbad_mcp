import asyncio
import json

async def list_conversations(page) -> str:
    print("Esperando a que cargue la lista de conversaciones de LinkedIn...")
    try:
        await page.wait_for_selector("li.msg-conversation-listitem, .msg-conversations-container__convo-item, [class*='msg-conversations-container__convo-item']", timeout=15000)
    except Exception:
        current_url = page.url
        if "linkedin.com/messaging" not in current_url:
            return f"Error: No parece estar en la página de mensajería (URL actual: {current_url}). Verifica si tienes la sesión iniciada."
        return "Error: No se cargaron elementos de conversación. Es posible que el chat esté vacío o el selector haya cambiado."

    await asyncio.sleep(2)  # Wait for full rendering

    # Extract conversations using evaluate on unique LI elements
    conversations = await page.evaluate("""() => {
        let items = Array.from(document.querySelectorAll('li.msg-conversation-listitem, li[class*="msg-conversation-listitem"]'));
        if (items.length === 0) {
            items = Array.from(document.querySelectorAll('.msg-conversations-container__convo-item, [class*="msg-conversations-container__convo-item"]'));
        }
        const result = [];
        const seenNames = new Set();

        items.forEach((item, index) => {
            const nameEl = item.querySelector('.msg-conversation-card__participant-names, [class*="participant-names"]');
            const imgEl = item.querySelector('img');
            const name = nameEl ? nameEl.textContent.trim() : (imgEl && imgEl.alt ? imgEl.alt.trim() : 'Unknown');

            const snippetEl = item.querySelector('.msg-conversation-card__message-snippet, [class*="message-snippet"]');
            const snippet = snippetEl ? snippetEl.textContent.trim() : '';

            const timeEl = item.querySelector('.msg-conversation-card__time-stamp, [class*="time-stamp"]');
            const time = timeEl ? timeEl.textContent.trim() : '';

            const isUnread = item.classList.contains('msg-conversations-container__convo-item--unread') || 
                             item.querySelector('[class*="unread"]') !== null ||
                             item.innerText.includes('unread') ||
                             item.innerText.includes('sin leer') ||
                             item.innerText.includes('Unread');

            const linkEl = item.querySelector('a[href*="/in/"]');
            const profileUrl = linkEl ? linkEl.href : null;

            const threadLinkEl = item.querySelector('a[href*="/messaging/thread/"]');
            const threadUrl = threadLinkEl ? threadLinkEl.href : null;

            result.push({
                index,
                name,
                snippet,
                time,
                isUnread,
                profileUrl,
                threadUrl
            });
        });
        return result;
    }""")

    return json.dumps(conversations, indent=2, ensure_ascii=False)


async def read_messages(page, contact_name: str = "", limit: int = 20) -> str:
    print("Cargando mensajería de LinkedIn...")
    try:
        await page.wait_for_selector("li.msg-conversation-listitem, .msg-conversations-container__convo-item, [class*='msg-conversations-container__convo-item']", timeout=15000)
    except Exception:
        if "linkedin.com/messaging" not in page.url:
            return f"Error: No estás en la página de mensajería (URL actual: {page.url}). Asegúrate de tener sesión iniciada."
        return "Error: No se pudo cargar la lista de conversaciones."

    await asyncio.sleep(2)

    if contact_name:
        contact_name_lower = contact_name.lower().strip()
        found = await page.evaluate("""(searchName) => {
            const items = document.querySelectorAll('li.msg-conversation-listitem, .msg-conversations-container__convo-item, [class*="msg-conversations-container__convo-item"]');
            for (let i = 0; i < items.length; i++) {
                const nameEl = items[i].querySelector('.msg-conversation-card__participant-names, [class*="participant-names"]');
                const imgEl = items[i].querySelector('img');
                const name = nameEl ? nameEl.textContent.toLowerCase() : (imgEl && imgEl.alt ? imgEl.alt.toLowerCase() : '');
                if (name && name.includes(searchName)) {
                    items[i].click();
                    return true;
                }
            }
            return false;
        }""", contact_name_lower)

        if not found:
            print(f"Contacto '{contact_name}' no encontrado en lista visible. Intentando buscar en el cuadro de búsqueda...")
            search_input = page.locator('.msg-messaging-search__search-field input, [placeholder*="Search messages"], [placeholder*="Buscar mensajes"], [placeholder*="Buscar"], [placeholder*="Search"]').first
            if await search_input.is_visible():
                await search_input.click()
                await search_input.fill(contact_name)
                await page.keyboard.press("Enter")
                await asyncio.sleep(4)
                
                found_in_search = await page.evaluate("""(searchName) => {
                    const items = document.querySelectorAll('li.msg-conversation-listitem, .msg-conversations-container__convo-item, [class*="msg-conversations-container__convo-item"]');
                    for (let i = 0; i < items.length; i++) {
                        const nameEl = items[i].querySelector('.msg-conversation-card__participant-names, [class*="participant-names"]');
                        const imgEl = items[i].querySelector('img');
                        const name = nameEl ? nameEl.textContent.toLowerCase() : (imgEl && imgEl.alt ? imgEl.alt.toLowerCase() : '');
                        if (name && name.includes(searchName)) {
                            items[i].click();
                            return true;
                        }
                    }
                    if (items.length > 0) {
                        items[0].click();
                        return true;
                    }
                    return false;
                }""", contact_name_lower)
                
                if not found_in_search:
                    return f"Error: No se encontró ninguna conversación con el contacto '{contact_name}'."
            else:
                return f"Error: No se encontró el contacto '{contact_name}' ni el cuadro de búsqueda."
        
        print(f"Chat con '{contact_name}' seleccionado. Esperando mensajes...")
        await asyncio.sleep(3)

    # Extract active messages cleanly
    messages = await page.evaluate("""() => {
        const items = Array.from(document.querySelectorAll('.msg-s-event-listitem, .msg-s-message-list-item, [class*="msg-s-event-listitem"]'))
            .filter(el => !el.classList.contains('visually-hidden') && el.tagName !== 'SPAN' && el.querySelector('.msg-s-event-listitem__body, [class*="event-listitem__body"], .msg-s-message-listitem__body, [class*="message-listitem__body"]'));
        
        const result = [];
        let currentSender = 'Unknown';
        let currentTimestamp = '';

        items.forEach(item => {
            const group = item.closest('.msg-s-message-group, [class*="msg-s-message-group"]') || item;
            const senderEl = group.querySelector('.msg-s-message-group__name, [class*="message-group__name"], [class*="profile-link"], .msg-s-event-listitem__link img[alt]');
            if (senderEl) {
                const name = senderEl.getAttribute('alt') || senderEl.textContent.trim();
                if (name) currentSender = name;
            }
            
            const timeEl = group.querySelector('.msg-s-message-group__timestamp, [class*="timestamp"], time');
            if (timeEl) {
                currentTimestamp = timeEl.textContent.trim();
            }

            const bodyEl = item.querySelector('.msg-s-event-listitem__body, [class*="event-listitem__body"], .msg-s-message-listitem__body, [class*="message-listitem__body"]');
            if (bodyEl) {
                const text = bodyEl.textContent.trim();
                if (text) {
                    result.push({
                        sender: currentSender,
                        text: text,
                        timestamp: currentTimestamp
                    });
                }
            }
        });
        return result;
    }""")

    trimmed_messages = messages[-limit:] if limit > 0 else messages
    return json.dumps(trimmed_messages, indent=2, ensure_ascii=False)


async def send_message_via_profile(page, profile_url: str, message: str, human_type) -> str:
    print(f"Abriendo perfil: {profile_url}...")
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(4)

    # Locate Message button
    message_btn = page.locator('.pvs-profile-actions button:has-text("Message"), .pvs-profile-actions button:has-text("Enviar mensaje"), .pvs-profile-actions button:has-text("Mensaje")').first
    if not await message_btn.is_visible():
        message_btn = page.locator('button:has-text("Message"), button:has-text("Enviar mensaje"), button:has-text("Mensaje")').first

    if await message_btn.is_visible():
        print("Haciendo clic en el botón de Mensaje del perfil...")
        await message_btn.click()
    else:
        more_btn = page.locator('button:has-text("More"), button:has-text("Más"), button[aria-label*="More options"], button[aria-label*="Más opciones"]').first
        if await more_btn.is_visible():
            print("Mensaje no visible. Abriendo menú 'Más'...")
            await more_btn.click()
            await asyncio.sleep(1.5)
            dropdown_btn = page.locator('div[role="dropdown"] button:has-text("Message"), ul button:has-text("Message"), div[role="dropdown"] button:has-text("Enviar mensaje"), ul button:has-text("Enviar mensaje")').first
            if await dropdown_btn.is_visible():
                await dropdown_btn.click()
            else:
                return "Error: No se pudo encontrar el botón de mensaje en el perfil, incluso tras abrir 'Más'."
        else:
            return "Error: No se encontró el botón de enviar mensaje en el perfil."

    print("Esperando a que se abra la ventana de chat...")
    await asyncio.sleep(3)

    textbox = page.locator('.msg-form__contenteditable, [contenteditable="true"], [role="textbox"]').last
    if not await textbox.is_visible():
        return "Error: No se pudo localizar el editor de texto en la ventana de chat abierta."

    await textbox.click()
    await textbox.focus()
    await human_type(textbox, message)
    await asyncio.sleep(1.5)

    send_btn = page.locator('.msg-form__send-button, button[type="submit"]:has-text("Send"), button[type="submit"]:has-text("Enviar"), button:has-text("Send"), button:has-text("Enviar")').last
    if await send_btn.is_visible() and not await send_btn.is_disabled():
        await send_btn.click()
        await asyncio.sleep(2)
        return f"✓ Mensaje enviado correctamente al perfil {profile_url}."
    else:
        return "Error: El botón de enviar mensaje está deshabilitado o no está visible."


async def send_message_via_messaging(page, contact_name: str, message: str, human_type) -> str:
    print("Abriendo mensajería de LinkedIn...")
    try:
        await page.wait_for_selector("li.msg-conversation-listitem, .msg-conversations-container__convo-item, [class*='msg-conversations-container__convo-item']", timeout=15000)
    except Exception:
        if "linkedin.com/messaging" not in page.url:
            return f"Error: No estás en la página de mensajería. URL actual: {page.url}."
        return "Error: No se cargó la lista de conversaciones."

    await asyncio.sleep(2)

    contact_name_lower = contact_name.lower().strip()
    found = await page.evaluate("""(searchName) => {
        const items = document.querySelectorAll('li.msg-conversation-listitem, .msg-conversations-container__convo-item, [class*="msg-conversations-container__convo-item"]');
        for (let i = 0; i < items.length; i++) {
            const nameEl = items[i].querySelector('.msg-conversation-card__participant-names, [class*="participant-names"]');
            const imgEl = items[i].querySelector('img');
            const name = nameEl ? nameEl.textContent.toLowerCase() : (imgEl && imgEl.alt ? imgEl.alt.toLowerCase() : '');
            if (name && name.includes(searchName)) {
                items[i].click();
                return true;
            }
        }
        return false;
    }""", contact_name_lower)

    if not found:
        print(f"Contacto '{contact_name}' no visible en lista. Buscando...")
        search_input = page.locator('.msg-messaging-search__search-field input, [placeholder*="Search messages"], [placeholder*="Buscar mensajes"], [placeholder*="Buscar"], [placeholder*="Search"]').first
        if await search_input.is_visible():
            await search_input.click()
            await search_input.fill(contact_name)
            await page.keyboard.press("Enter")
            await asyncio.sleep(4)

            found_in_search = await page.evaluate("""(searchName) => {
                const items = document.querySelectorAll('li.msg-conversation-listitem, .msg-conversations-container__convo-item, [class*="msg-conversations-container__convo-item"]');
                for (let i = 0; i < items.length; i++) {
                    const nameEl = items[i].querySelector('.msg-conversation-card__participant-names, [class*="participant-names"]');
                    const imgEl = items[i].querySelector('img');
                    const name = nameEl ? nameEl.textContent.toLowerCase() : (imgEl && imgEl.alt ? imgEl.alt.toLowerCase() : '');
                    if (name && name.includes(searchName)) {
                        items[i].click();
                        return true;
                    }
                }
                if (items.length > 0) {
                    items[0].click();
                    return true;
                }
                return false;
            }""", contact_name_lower)

            if not found_in_search:
                print("No se encontró conversación existente. Intentando crear nuevo mensaje...")
                compose_btn = page.locator('a[href*="/messaging/thread/new"], button[aria-label*="Compose"], button[aria-label*="Redactar"], [class*="compose"] button, [class*="compose"] a').first
                if await compose_btn.is_visible():
                    await compose_btn.click()
                    await asyncio.sleep(2)
                    
                    to_input = page.locator('input[name="search"], input[placeholder*="Type a name"], input[placeholder*="Escribe un nombre"], [role="combobox"]').first
                    if await to_input.is_visible():
                        await to_input.click()
                        await to_input.fill(contact_name)
                        await asyncio.sleep(3)
                        
                        option = page.locator('[role="option"], .msg-connections-typeahead__result-item').first
                        if await option.is_visible():
                            await option.click()
                            await asyncio.sleep(2)
                        else:
                            return f"Error: No se encontró contacto autocomplete para '{contact_name}'."
                    else:
                        return "Error: No se pudo localizar el campo 'Para' (destinatario) en el nuevo mensaje."
                else:
                    return f"Error: No se pudo encontrar al contacto '{contact_name}'."

    print("Preparando editor de mensajes...")
    await asyncio.sleep(2)

    textbox = page.locator('.msg-form__contenteditable, [contenteditable="true"], [role="textbox"]').first
    if not await textbox.is_visible():
        return "Error: No se pudo encontrar el editor de texto del mensaje."

    await textbox.click()
    await textbox.focus()
    await human_type(textbox, message)
    await asyncio.sleep(1.5)

    send_btn = page.locator('.msg-form__send-button, button[type="submit"]:has-text("Send"), button[type="submit"]:has-text("Enviar"), button:has-text("Send"), button:has-text("Enviar")').last
    if await send_btn.is_visible() and not await send_btn.is_disabled():
        await send_btn.click()
        await asyncio.sleep(2)
        return f"✓ Mensaje enviado correctamente a '{contact_name}'."
    else:
        return "Error: El botón de enviar está deshabilitado o no visible."

