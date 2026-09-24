"""
ClickUp API wrapper — genérico, sin comandos fijos ni datos embedidos.
Uso como script: python3 clickup.py <api_key> <method> <path> [data_json]
Ejemplos:
  python3 clickup.py pk_... GET /team
  python3 clickup.py pk_... GET /team/90132707763/space
  python3 clickup.py pk_... GET /space/901313864337/folder
  python3 clickup.py pk_... GET /list/1000500000001319/task
  python3 clickup.py pk_... PUT /list/1000500000001319 '{"override_statuses":true,"statuses":[]}'
  python3 clickup.py pk_... POST /list/1000500000001319/task '{"name":"Test"}'
"""

import urllib.request, json, sys, os

BASE = "https://api.clickup.com/api/v2"


def api(api_key, method="GET", path="", data=None):
    """Llama cualquier endpoint de ClickUp REST API."""
    url = f"{BASE}{path}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    
    req = urllib.request.Request(url, method=method.upper(), data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"})
    
    try:
        with urllib.request.urlopen(req) as r:
            resp = r.read().decode("utf-8")
            # Intentar parsear JSON, si no es JSON devolver raw
            try:
                return json.loads(resp)
            except json.JSONDecodeError:
                return resp
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")[:500]
        return {"_error": f"HTTP {e.code}", "_detail": err_body}
    except urllib.error.URLError as e:
        return {"_error": f"Connection error: {e.reason}"}


def pretty(obj):
    """Formatea JSON para legibilidad."""
    if isinstance(obj, dict) and "_error" in obj:
        return f"❌ {obj['_error']}\n{obj.get('_detail', '')}"
    if isinstance(obj, str):
        return obj[:2000]
    return json.dumps(obj, indent=2, ensure_ascii=False)[:3000]


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    
    key = sys.argv[1]
    method = sys.argv[2].upper()
    path = sys.argv[3]
    data = json.loads(sys.argv[4]) if len(sys.argv) > 4 else None
    
    result = api(key, method, path, data)
    print(pretty(result))
