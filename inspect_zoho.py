import asyncio
from playwright.async_api import async_playwright
import json

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9227")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        url = "https://forms.zohopublic.com/utecadmin/form/HackathonVivienda/formperma/LPbaip9s64qXtNlHMegyL4U9TfTEQ2OWsWDsVeODqgU"
        print(f"Loading {url}...")
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        # Inspect all form elements
        data = await page.evaluate('''() => {
            // Get all field containers in Zoho forms
            // Zoho Forms uses div[eltype] or li[eltype] or .zf-tempContDiv
            const allElements = document.querySelectorAll('[eltype], .zf-tempContDiv, .tempContDiv, form li');
            
            const results = [];
            document.querySelectorAll('li').forEach(li => {
                const eltype = li.getAttribute('eltype') || '';
                const compname = li.getAttribute('compname') || '';
                const label = li.querySelector('label, .labelName, .flFlt')?.innerText?.trim() || '';
                const isMandatory = !!li.querySelector('em.mandate, .mandatory') || label.includes('*');
                const instruct = li.querySelector('.instruct, .field-desc, p.fieldDescription')?.innerText?.trim() || '';
                
                const inputs = Array.from(li.querySelectorAll('input, select, textarea')).map(inp => {
                    let opts = [];
                    if (inp.tagName === 'SELECT') {
                        opts = Array.from(inp.options).map(o => ({ text: o.text.trim(), value: o.value }));
                    }
                    return {
                        tag: inp.tagName,
                        type: inp.type || '',
                        name: inp.name || '',
                        id: inp.id || '',
                        placeholder: inp.placeholder || '',
                        options: opts
                    };
                });
                
                const choices = Array.from(li.querySelectorAll('.choiceLabel, .radio-label, .checkbox-label, span.choice-text, .zf-choiceWrapper label')).map(c => c.innerText.trim());

                if (label || inputs.length > 0 || eltype) {
                    results.push({
                        eltype,
                        compname,
                        label,
                        isMandatory,
                        instruct,
                        inputs,
                        choices,
                        fullText: li.innerText.trim()
                    });
                }
            });
            return results;
        }''')
        
        print(f"Found {len(data)} field items.")
        with open("/tmp/zoho_full_fields.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            
        for i, item in enumerate(data):
            print(f"\n--- ITEM {i+1} ---")
            print(f"Label: {item['label']}")
            print(f"Mandatory: {item['isMandatory']}")
            print(f"Instruct: {item['instruct']}")
            if item['choices']:
                print(f"Choices: {item['choices']}")
            for inp in item['inputs']:
                if inp['options']:
                    print(f"Select ({inp['name']}): {[o['text'] for o in inp['options']]}")
                else:
                    print(f"Input: tag={inp['tag']}, type={inp['type']}, name={inp['name']}, placeholder={inp['placeholder']}")

        await page.close()

if __name__ == "__main__":
    asyncio.run(main())
