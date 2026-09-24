import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        try:
            # Connect to Chromium CDP (port 9227 is mapped/available via network=host)
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9227")
            contexts = browser.contexts
            if not contexts:
                print("Error: No contexts found")
                return
            context = contexts[0]
            pages = context.pages
            linkedin_pages = [p for p in pages if "linkedin.com" in p.url]
            if not linkedin_pages:
                print("Error: No LinkedIn page found in browser.")
                return
            page = linkedin_pages[0]
            await page.bring_to_front()
            
            # Locate the textbox
            textbox = page.locator('div[role="textbox"]')
            await textbox.focus()
            await textbox.click()
            
            # Clear it first using select all + backspace
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.5)
            
            # Type the comment natively
            comment_text = "Gran hito, Martín. Romper la inercia del mercado operando y cerrando tracción real antes de validar con teorías es la única solución eficiente. Felicidades por la adquisición."
            await page.keyboard.type(comment_text, delay=35)
            await asyncio.sleep(1.5)
            
            # Locate and click submit button
            submit_btn = page.locator('button[type="submit"]')
            await submit_btn.wait_for(state="visible")
            is_disabled = await submit_btn.is_disabled()
            if is_disabled:
                print("Warning: Submit button is still disabled.")
            
            await submit_btn.click()
            await asyncio.sleep(2)
            print("Success")
        except Exception as e:
            print(f"Error: {e}")

asyncio.run(run())
