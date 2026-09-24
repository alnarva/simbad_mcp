import asyncio
from playwright.async_api import async_playwright
import json

async def parse_all():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9227")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        await page.goto("https://forms.zohopublic.com/utecadmin/form/HackathonVivienda/formperma/LPbaip9s64qXtNlHMegyL4U9TfTEQ2OWsWDsVeODqgU", wait_until="networkidle")
        
        data = await page.evaluate("""() => {
            const list = [];
            document.querySelectorAll(".tempContDiv").forEach((el, index) => {
                const label = el.querySelector(".fieldlabel, .labelName")?.innerText?.trim() || "";
                const instruction = el.querySelector(".instruct, .field-desc, p.fieldDescription")?.innerText?.trim() || "";
                const isRequired = el.innerText.includes("*") || !!el.querySelector("em, .mandatory");
                
                const inputs = [];
                el.querySelectorAll("input, select, textarea").forEach(inp => {
                    const tag = inp.tagName.toLowerCase();
                    const type = inp.type || tag;
                    const name = inp.name || "";
                    let opts = [];
                    if (tag === "select") {
                        opts = Array.from(inp.options).map(o => o.text.trim()).filter(Boolean);
                    }
                    inputs.push({ tag, type, name, opts });
                });
                
                const choices = Array.from(el.querySelectorAll(".radioChoice, .choiceLabel, .checkbox-label, .zf-choiceWrapper label")).map(c => c.innerText.trim()).filter(Boolean);
                
                if (label || choices.length > 0 || inputs.length > 0) {
                    list.push({
                        index: index + 1,
                        label: label.replace(/\\*\\s*Required$/, "").replace(/\\*$/, "").trim(),
                        isRequired,
                        instruction,
                        choices,
                        inputs,
                        raw: el.innerText.trim()
                    });
                }
            });
            return list;
        }""")
        
        for d in data:
            idx = d["index"]
            lbl = d["label"]
            req = "OBLIGATORIO" if d["isRequired"] else "OPCIONAL"
            inst = d["instruction"]
            choi = d["choices"]
            print(f"[{idx}] {lbl} ({req})")
            if inst:
                print(f"    Instrucción: {inst}")
            if choi:
                print(f"    Opciones: {choi}")
            for inp in d["inputs"]:
                name = inp["name"]
                opts = inp["opts"]
                tag = inp["tag"]
                itype = inp["type"]
                if opts:
                    print(f"    Select ({name}): {opts}")
                elif tag != "input" or itype not in ["radio", "checkbox"]:
                    print(f"    Input field: {tag} ({itype}) - {name}")
                    
        await page.close()

if __name__ == "__main__":
    asyncio.run(parse_all())
