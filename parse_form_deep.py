import asyncio
from playwright.async_api import async_playwright
import json

async def parse_form():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://localhost:9227")
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
        await page.goto("https://forms.zohopublic.com/utecadmin/form/HackathonVivienda/formperma/LPbaip9s64qXtNlHMegyL4U9TfTEQ2OWsWDsVeODqgU", wait_until="networkidle")
        
        js_code = """
        () => {
            const form = document.querySelector('form');
            if (!form) return { error: 'No form found' };
            
            const questions = [];
            form.querySelectorAll('label.fieldlabel').forEach((lbl, idx) => {
                const parent = lbl.parentElement;
                const labelText = lbl.innerText.trim();
                const inputs = Array.from(parent.querySelectorAll('input, select, textarea')).map(i => {
                    let opts = [];
                    if (i.tagName === 'SELECT') {
                        opts = Array.from(i.options).map(o => o.text.trim()).filter(Boolean);
                    }
                    return {
                        tag: i.tagName,
                        type: i.type,
                        name: i.name,
                        id: i.id,
                        placeholder: i.placeholder || '',
                        options: opts
                    };
                });
                const choices = Array.from(parent.querySelectorAll('label.radioChoice, label.choiceLabel, .checkbox-label, .zf-choiceWrapper label, label.cusChoiceLabel')).map(c => c.innerText.trim()).filter(Boolean);
                const instruction = parent.querySelector('.instruct, .field-desc, p.fieldDescription, .instruction')?.innerText?.trim() || '';
                
                questions.push({
                    idx: idx + 1,
                    label: labelText,
                    instruction: instruction,
                    inputs: inputs,
                    choices: choices,
                    parentText: parent.innerText.trim()
                });
            });
            
            return {
                formId: form.id,
                questionsCount: questions.length,
                questions: questions
            };
        }
        """
        data = await page.evaluate(js_code)
        
        print("Questions found:", data.get("questionsCount", 0))
        for q in data.get("questions", []):
            print(f"\n================ QUESTION {q['idx']} ================")
            print(f"LABEL: {q['label']}")
            if q['instruction']:
                print(f"INSTRUCTION: {q['instruction']}")
            if q['choices']:
                print(f"CHOICES: {q['choices']}")
            for inp in q['inputs']:
                if inp['options']:
                    print(f"SELECT [{inp['name']}]: {inp['options']}")
                elif inp['tag'] != 'INPUT' or inp['type'] not in ['radio', 'checkbox']:
                    print(f"INPUT [{inp['tag']}/{inp['type']}]: name={inp['name']}, placeholder={inp['placeholder']}")

        with open("/tmp/extracted_form_data.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            
        await page.close()

if __name__ == "__main__":
    asyncio.run(parse_form())
