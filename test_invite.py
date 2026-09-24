import asyncio
import json
from playwright.async_api import async_playwright
import simbad_mcp

async def inspect():
    simbad_mcp.CURRENT_ENGINE = "chromium"
    cdp_url = simbad_mcp.get_cdp_url()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        page = await simbad_mcp.get_single_linkedin_page(context)
        
        target_url = "https://www.linkedin.com/company/115807411/admin/dashboard/?invite=true"
        print(f"Navigating to {target_url}...")
        await page.goto(target_url)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(4)
        
        info = await page.evaluate("""() => {
            const modal = document.querySelector('div[role="dialog"]') || document.querySelector('.artdeco-modal');
            if (!modal) return { error: "no modal" };
            
            const inputs = Array.from(modal.querySelectorAll('input[type="checkbox"]')).map(i => ({
                id: i.id,
                checked: i.checked,
                ariaLabel: i.getAttribute("aria-label") || ""
            }));
            
            const labels = Array.from(modal.querySelectorAll('label')).map(l => ({
                text: (l.innerText || "").trim(),
                forAttr: l.getAttribute("for") || ""
            }));
            
            const buttons = Array.from(modal.querySelectorAll('button')).map(b => ({
                text: (b.innerText || "").trim().replace("\\n", " "),
                ariaLabel: b.getAttribute("aria-label") || "",
                disabled: b.disabled,
                class: b.className
            }));
            
            return { inputsCount: inputs.length, inputs: inputs.slice(0, 10), labels: labels.slice(0, 10), buttons };
        }""")
        
        print("Modal Info:\n", json.dumps(info, indent=2))

if __name__ == "__main__":
    asyncio.run(inspect())
