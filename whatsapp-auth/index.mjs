import puppeteer from 'puppeteer-core';
import qrcode from 'qrcode-terminal';

(async () => {
    try {
        console.log("🚀 Iniciando Chromium real en modo headless...");
        const browser = await puppeteer.launch({
            executablePath: '/usr/bin/chromium', // Usamos Chromium real para engañar a WhatsApp
            headless: 'new', // Headless moderno
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
            ],
            // Usamos la carpeta local para evitar problemas de permisos de root (Docker)
            userDataDir: '/home/alnar/Projects/MCP_SIMBAD/whatsapp-auth/db' 
        });
        
        console.log("🌐 Navegando a WhatsApp Web...");
        const page = await browser.newPage();
        
        // Evitamos el bloqueo cambiando el user agent por precaución adicional
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
        
        await page.goto('https://web.whatsapp.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });

        console.log("⌛ Esperando a que cargue el código QR (esto puede tomar un momento)...");
        // Buscamos el elemento que contiene el string del QR
        await page.waitForSelector('div[data-ref]', { timeout: 60000 });
        
        const qrData = await page.evaluate(() => {
            const el = document.querySelector('div[data-ref]');
            return el ? el.getAttribute('data-ref') : null;
        });

        if (qrData) {
            console.log("\n=======================================================");
            console.log("📲 ESCANEA ESTE CÓDIGO QR CON LA APP DE WHATSAPP:");
            console.log("=======================================================\n");
            // Imprime el QR directamente en la consola
            qrcode.generate(qrData, {small: true});
            
            console.log("\nEsperando a que escanees el código (tienes unos 2 minutos)...");
            // pane-side es el panel lateral izquierdo que solo aparece cuando ya hay sesión iniciada
            await page.waitForSelector('#pane-side', { timeout: 120000 });
            console.log("✅ ¡Sesión vinculada correctamente! Credenciales guardadas en obscura-profile.");
        } else {
            console.log("❌ No se generó el código QR (probablemente ya haya una sesión activa).");
        }

        // Cerramos la página y el navegador para soltar los bloqueos de los archivos
        await page.close();
        await browser.close();
        process.exit(0);
    } catch (e) {
        console.error("Error en la ejecución:", e);
        process.exit(1);
    }
})();
