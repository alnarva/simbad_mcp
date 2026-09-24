import asyncio
from playwright.async_api import async_playwright
import json

async def parse_all():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9227")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        await page.goto("https://forms.zohopublic.com/utecadmin/form/HackathonVivienda/formperma/LPbaip9s64qXtNlHMegyL4U9TfTEQ2OWsWDsVeODqgU", wait_until="networkidle")
        
        # Let us extract every field wrapper in Zoho
        data = await page.evaluate("""() => {
            const form = document.querySelector("form");
            const allNodes = [];
            
            // Find all children or sections
            const fieldContainers = document.querySelectorAll("li[eltype], .zf-tempContDiv, li");
            
            // Let's get every label and its associated input/select/textarea
            const rows = [];
            document.querySelectorAll("li").forEach((li, idx) => {
                const text = li.innerText.trim();
                const label = li.querySelector(".fieldlabel, label, .labelName")?.innerText?.trim() || "";
                const instruction = li.querySelector(".instruct, .field-desc, p.fieldDescription, .instruction")?.innerText?.trim() || "";
                const inputs = Array.from(li.querySelectorAll("input, select, textarea")).map(i => ({
                    tag: i.tagName,
                    type: i.type,
                    name: i.name,
                    id: i.id,
                    placeholder: i.placeholder || "",
                    value: i.value || "",
                    options: i.tagName === "SELECT" ? Array.from(i.options).map(o => o.text.trim()).filter(Boolean) : []
                }));
                
                const choices = Array.from(li.querySelectorAll(".radioChoice, .choiceLabel, .checkbox-label, .zf-choiceWrapper label, label.cusChoiceLabel")).map(c => c.innerText.trim()).filter(Boolean);
                
                if (text.length > 0) {
                    rows.push({
                        idx,
                        label: label || text.split("\\n")[0],
                        instruction,
                        choices,
                        inputs,
                        fullText: text
                    });
                }
            });
            return rows;
        }""")
        
        print("Total rows:", len(data))
        for r in data:
            print(f"\n================ ROW {r['idx']} ================")
            print(f"LABEL: {r['label']}")
            if r['instruction']:
                print(f"INSTRUCTION: {r['instruction']}")
            if r['choices']:
                print(f"CHOICES: {r['choices']}")
            for inp in r['inputs']:
                if inp['options']:
                    print(f"SELECT [{inp['name']}]: {inp['options']}")
                else:
                    print(f"INPUT [{inp['tag']} / {inp['type']}]: name={inp['name']}, placeholder={inp['placeholder']}")
            print(f"TEXT:\n{r['fullText']}")

        await page.close()

if __name__ == "__main__":
    asyncio.run(parse_all())
