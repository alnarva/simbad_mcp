# Plan de Desarrollo — Retina Semántica para Simbad MCP

## 🎯 Objetivo
Dotar a **Simbad MCP** de capacidades de navegación espacial y compresión visual para permitir que **modelos de lenguaje de texto puro (modelos "ciegos" como DeepSeek v4, DeepSeek R1 o LLaMA 3)** puedan "ver", mapear e interactuar con aplicaciones web y la terminal con la misma precisión que un modelo multimodal.

---

## 🏗️ Arquitectura de la Retina Semántica

```
                    ┌─────────────────────────────────────────┐
                    │      Modelo Ciego (DeepSeek v4 / R1)    │
                    └────────────────────┬────────────────────┘
                                         │
                                   Llamada MCP
                                         │
                                         ▼
                    ┌─────────────────────────────────────────┐
                    │               Simbad MCP                │
                    └────┬───────────────┼───────────────┬────┘
                         │               │               │
                         ▼               ▼               ▼
                  ┌────────────┐  ┌─────────────┐  ┌─────────────┐
                  │ Módulo A11y│  │ Módulo Map  │  │ Módulo OCR/ │
                  │    Tree    │  │ Espacial UI │  │ Vision Cop. │
                  └─────┬──────┘  └──────┬──────┘  └──────┬──────┘
                        │                │                │
                        ▼                ▼                ▼
                 [Playwright AX]   [DOM JS Bounds]  [Ollama/Paddle]
```

---

## 📅 Fases de Implementación

### Fase 1: Módulo de Árbol de Accesibilidad (A11y Tree)
**Propósito:** Convertir la jerarquía visual del navegador a un árbol lógico compacto optimizado para tokens.

* **Nueva Herramienta MCP:** `get_accessibility_tree(engine: str = "chromium", interesting_only: bool = True) -> str`
* **Mecánica:**
  1. Utiliza `page.accessibility.snapshot(interesting_only=True)` de Playwright.
  2. Filtra nodos invisibles, vacíos o redundantes.
  3. Formatea la salida en YAML/JSON indentado con los roles (`button`, `textbox`, `link`), nombres accesibles y estados (`focused`, `disabled`).
* **Entregable:** Un mapa semántico de la página listo para ser leído por el modelo en < 500 tokens.

---

### Fase 2: Mapeo Espacial e Interactivo de Elementos (Spatial Map)
**Propósito:** Asignar identificadores numéricos simples y coordenadas físicas a cada elemento interactivo de la pantalla.

* **Nuevas Herramientas MCP:**
  * `get_interactive_map(engine: str = "chromium") -> str`
  * `page_click_by_id(element_id: int, engine: str = "chromium") -> str`
* **Mecánica:**
  1. Inyecta un script de JavaScript que busca todos los elementos interactivos (`a`, `button`, `input`, `select`, `textarea`, `[role="button"]`).
  2. Asigna un ID numérico (`[1]`, `[2]`, `[3]`) y calcula su caja de límites (`bounding_box`: `x, y, width, height`).
  3. Devuelve una lista estructurada:
     ```yaml
     [1] Button "Iniciar Sesión" (x: 120, y: 350)
     [2] Input Text "Usuario" (x: 120, y: 200) -> valor actual: ""
     [3] Link "Olvidé mi contraseña" (x: 120, y: 390)
     ```
  4. Permite al modelo ciego ejecutar `page_click_by_id(1)` en lugar de lidiar con selectores CSS complejos que se rompen.

---

### Fase 3: Copiloto Visual Local y OCR (Visual Descriptor)
**Propósito:** Interpretar imágenes, gráficos o áreas de pantalla no estructuradas sin depender de APIs en la nube.

* **Nueva Herramienta MCP:** `describe_screenshot(engine: str = "chromium", target_selector: str = None) -> str`
* **Mecánica:**
  1. Captura la pantalla completa o la sección definida por `target_selector`.
  2. Envía la imagen al motor local de Ollama llamando a un modelo de visión compacto (ej. `qwen2-vl` o `llama3.2-vision`) o ejecuta un motor local de OCR (`PaddleOCR` / `pytesseract`).
  3. Devuelve al modelo principal (DeepSeek) una descripción textual detallada del contenido visual o la transcripción formateada de la imagen.

---

### Fase 4: Caché e Integración con Memoria Semántica (Atlántico)
**Propósito:** Evitar re-procesamientos de pantalla y mantener consistencia de estado.

* **Mecánica:**
  1. Guardar el mapa espacial y el árbol A11y generado en la base de datos SQLite `web_context` (Atlántico) asociándolo al URL y hash de contenido.
  2. Implementar un TTL (Time-To-Live) de 3 minutos: si la página no ha cambiado, sirve el mapa desde caché local en milisegundos.

---

## 🛠️ Modificaciones Técnicas en la Estructura del Código

1. **[simbad_mcp.py](file:///home/alnar/Projects/MCP_SIMBAD/simbad_mcp.py):**
   * Añadir los decoradores `@mcp.tool()` para `get_accessibility_tree`, `get_interactive_map`, `page_click_by_id` y `describe_screenshot`.
   * Integrar helpers JS para la extracción de `bounding_box` e ID asignados.
2. **[skills/simbad/SKILL.md](file:///home/alnar/Projects/MCP_SIMBAD/skills/simbad/SKILL.md):**
   * Actualizar la documentación de la Skill registrando las nuevas herramientas de Retina Semántica y ejemplos de workflows para modelos de texto puro.

---

## 🚀 Criterios de Éxito
* Un modelo de lenguaje puramente textual (sin capacidades de visión) es capaz de completar un formulario de 5 campos y hacer clic en el botón de confirmación en LinkedIn/Web con un 100% de tasa de éxito utilizando únicamente `get_interactive_map` y `page_click_by_id`.
