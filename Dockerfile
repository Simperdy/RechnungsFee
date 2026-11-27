# RechnungsFee Server — Docker-Image (multistage)
#
# Stage 1: Frontend bauen (node:22-alpine)
# Stage 2: Backend auf python:3.12-slim, non-root, /data als Volume
#
# OCR (tesseract) und Ghostscript (ocrmypdf) sind optionale Systempakete:
# Das Backend erkennt fehlendes tesseract und meldet "TESSERACT_FEHLT" —
# die App bleibt ohne OCR voll funktionsfaehig (nur gescannte PDFs fallen zurueck).
# Zum Aktivieren einkommentieren: apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu ghostscript

# ── Stage 1: Frontend-Build ────────────────────────────────────────────────
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY src/frontend/package.json src/frontend/package-lock.json ./
RUN npm ci
COPY src/frontend/ ./
RUN npm run build

# ── Stage 2: Runtime ───────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Optionale Systempakete (OCR/PDF): siehe Kommentar oben.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#       tesseract-ocr tesseract-ocr-deu ghostscript \
#   && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    RECHNUNGSFEE_SERVER_MODE=1 \
    RECHNUNGSFEE_BASE_DIR=/data

WORKDIR /app/src/backend

COPY src/backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Backend-Code
COPY src/backend/ ./
# Gebautes Frontend -> wird von server_mode.py als SPA_DIR ausgeliefert
COPY --from=frontend-build /build/dist ./static

# Non-root-User (numerisch, damit auch fremde Volume-UIDs passen)
RUN useradd -m -u 10001 rechnungsfee \
    && mkdir -p /data \
    && chown -R 10001:10001 /data /app
USER 10001:10001

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# Proxy-Header nur von privaten Docker-Netzen vertrauen (NPM sitzt dort). Port 8000
# darf nie direkt vom Host erreichbar sein - sonst liessen sich X-Forwarded-For/
# X-Forwarded-Proto spoofen. Im Server-Modus setzt das Backend das Secure-Flag
# sowieso hart (api/auth.py::ist_https).
# Forwarded-Header nur vom konkreten Proxy-Netz (NPM in proxy_sensitive, 172.16.15.0/24)
# und Loopback — nicht allen RFC1918-Netzen (sonst X-Forwarded-For/-Proto spoofbar).
# Proxy-Vertrauensbereich als Env uebergebbar (Default: Loopback + NPM-Netze
# proxy_sensitive/proxy_public). Compose setzt RECHNUNGSFEE_PROXY_NETZE.
ENV RECHNUNGSFEE_PROXY_NETZE="127.0.0.1,172.16.15.0/24,172.16.16.0/24"
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"$RECHNUNGSFEE_PROXY_NETZE\""]
