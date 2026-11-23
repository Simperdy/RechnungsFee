"""Server-Modus: Env-Flags und SPA-Auslieferung.

Aktiviert nur ueber RECHNUNGSFEE_SERVER_MODE=1 (Docker-Image setzt das).
Im Desktop-Modus (Tauri) bleibt alles beim Alten - dieses Modul wird dann
nicht verwendet.
"""
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

SERVER_MODE = os.environ.get("RECHNUNGSFEE_SERVER_MODE") == "1"

# Vom Dockerfile dorthin kopiert: <backend>/static = vite build dist/
SPA_DIR = Path(__file__).resolve().parent / "static"

# Pfade, die NIEMALS an den SPA-Fallback gehen duerfen
_API_PREFIX = "/api/"
_PUBLIC_PREFIXES = ("/api/auth/", "/health", "/docs", "/redoc", "/openapi.json")


def mount_spa(app: FastAPI) -> None:
    """Montiert die gebaute SPA als Fallback.

    Muss NACH allen API-Routern aufgerufen werden. Nicht-API-GET-Requests
    ohne Datei-Match liefern index.html (React-Router uebernimmt).
    """
    if not SERVER_MODE or not SPA_DIR.is_dir():
        return

    @app.exception_handler(404)
    async def spa_fallback(_request: Request, _exc: Exception) -> FileResponse | JSONResponse:
        path = _request.url.path
        if path.startswith(_API_PREFIX) or path in _PUBLIC_PREFIXES:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index = SPA_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    # Echte statische Assets (JS/CSS/Icons) direkt ausliefern
    app.mount("/assets", StaticFiles(directory=SPA_DIR / "assets"), name="assets")
    for name in ("favicon.png", "manifest.webmanifest", "sw.js", "icon-192.png",
                 "icon-512.png", "apple-touch-icon.png", "theme-init.js"):
        f = SPA_DIR / name
        if f.is_file():
            app.add_api_route(f"/{name}", lambda _f=f: FileResponse(_f), methods=["GET"], include_in_schema=False)
