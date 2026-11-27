"""Gemeinsamer Upload-Size-Guard fuer den Server-Modus (Docker).

Im Desktop-Modus (Tauri, lokaler Rechner des Nutzers) bleibt alles unveraendert.
Im Server-Modus duerfen Uploads nicht unbegrenzt in den RAM gelesen werden -
kein Memory-Limit im Container wuerde einen einzelnen grossen Upload zum
Container-Kill machen (DoS durch eingeloggten Nutzer oder kompromittiertes
Session-Cookie).

Verwendung an jeder Upload-Stelle statt ``await datei.read()``::

    from utils.upload_guard import lies_upload
    inhalt = await lies_upload(datei)
"""
from __future__ import annotations

import os

from fastapi import HTTPException, UploadFile

# 25 MB: reicht fuer Belege, E-Rechnungen (ZUGFeRD/XRechnung) und CSV-Importe
# selbst mit eingebetteten Anhaengen; alles Darueber ist ein Angriff oder ein
# Bedienfehler und bekommt eine klare 413-Antwort statt einem RAM-Overcommit.
MAX_UPLOAD_BYTES = int(os.environ.get("RECHNUNGSFEE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


async def lies_upload(datei: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Liest einen Upload komplett ein, begrenzt auf ``max_bytes``.

    Prueft zuerst ``Content-Length`` (sofort 413 ohne Lesen) und zaehlt beim
    Lesen mit (chunked Uploads ohne Content-Length). Nur im Server-Modus aktiv;
    im Desktop-Modus wird nur das Limit gegen unplausible Werte geprueft, das
    Verhalten bleibt ``await datei.read()``-kompatibel."""
    import server_mode

    if server_mode.SERVER_MODE:
        declared = datei.headers.get("content-length") if hasattr(datei, "headers") else None
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload zu gross ({int(declared)} Bytes, Limit {max_bytes}).",
                    )
            except ValueError:
                pass
        chunks: list[bytes] = []
        gesamt = 0
        while True:
            chunk = await datei.read(1024 * 1024)
            if not chunk:
                break
            gesamt += len(chunk)
            if gesamt > max_bytes:
                await datei.close()
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload zu gross (Limit {max_bytes} Bytes).",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    # Desktop-Modus: unveraendertes Verhalten
    return await datei.read()
