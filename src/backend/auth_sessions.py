"""Server-Modus: serverseitige Sessions fuer OIDC-Login.

Eigene Tabelle statt ``database/models.py`` - die GoBD-kritischen Modelle bleiben
unberuehrt. Die Tabelle wird im Server-Modus vom Startup in ``main.py`` nur einzeln
mit angelegt: ``Base.metadata.create_all(bind=engine, tables=[AuthSession.__table__])``.

Sicherheit:
- Die Session-ID (SID) ist ein 256-Bit-Zufallswert und wird NIE gespeichert - in der
  DB steht nur ``sid_hash`` (SHA-256 hex).
- Abgelaufene und revokierte Sessions gelten als ungueltig.
- Sliding Renewal: gueltige Nutzung verlaengert ``expires_at`` auf jetzt + 8 h,
  aber hoechstens einmal pro Stunde (kein DB-Write pro Request).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, Integer, String, select
from sqlalchemy.orm import Session

from database.connection import Base

COOKIE_NAME = "rf_session"
SESSION_TTL = timedelta(hours=8)
# Renewal erst nach 1 h ohne Verlaengerung schreiben (begrenzt DB-Writes)
RENEWAL_INTERVALL = timedelta(hours=1)

_SID_BYTES = 32  # 256 Bit


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True)
    sid_hash = Column(String(64), unique=True, nullable=False, index=True)  # SHA-256 hex
    email = Column(String(320), nullable=False)
    created_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)
    # Nur fuer den Ausbau (IdP-Logout) - aus den id_token-Claims sub/sid
    idp_sub = Column(String(255), nullable=True)
    idp_session = Column(String(255), nullable=True)


def _jetzt() -> datetime:
    """Naive UTC-Zeit: SQLite speichert keine Zeitzonen, Mischformen wuerden beim
    Vergleich mit gespeicherten Werten fehlschlagen."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def create_session(
    db: Session,
    email: str,
    *,
    idp_sub: str | None = None,
    idp_session: str | None = None,
    ttl: timedelta = SESSION_TTL,
) -> tuple[str, AuthSession]:
    """Legt eine neue Session an. Rueckgabe: (Klartext-SID nur fuer das Cookie, Zeile)."""
    sid = secrets.token_urlsafe(_SID_BYTES)
    jetzt = _jetzt()
    zeile = AuthSession(
        sid_hash=sid_hash(sid),
        email=email.strip().lower(),
        created_at=jetzt,
        expires_at=jetzt + ttl,
        idp_sub=idp_sub,
        idp_session=idp_session,
        revoked_at=None,
    )
    db.add(zeile)
    db.commit()
    db.refresh(zeile)
    return sid, zeile


def get_session(db: Session, sid: str | None) -> AuthSession | None:
    """Gueltige (nicht abgelaufene, nicht revokierte) Session zur SID - sonst None.
    Verlaengert ``expires_at`` per Sliding Renewal (gedrosselt durch RENEWAL_INTERVALL)."""
    if not sid:
        return None
    zeile = db.execute(
        select(AuthSession).where(AuthSession.sid_hash == sid_hash(sid))
    ).scalar_one_or_none()
    jetzt = _jetzt()
    if zeile is None:
        return None
    if zeile.revoked_at is not None or zeile.expires_at <= jetzt:
        return None
    if zeile.expires_at - jetzt < SESSION_TTL - RENEWAL_INTERVALL:
        zeile.expires_at = jetzt + SESSION_TTL
        db.commit()
    return zeile


def revoke_session(db: Session, sid: str | None) -> AuthSession | None:
    """Setzt ``revoked_at`` (idempotent). Rueckgabe: die Zeile, falls gefunden."""
    if not sid:
        return None
    zeile = db.execute(
        select(AuthSession).where(AuthSession.sid_hash == sid_hash(sid))
    ).scalar_one_or_none()
    if zeile is not None and zeile.revoked_at is None:
        zeile.revoked_at = _jetzt()
        db.commit()
    return zeile


def purge_abgelaufen(db: Session, *, behalte_tage: int = 30) -> int:
    """Loescht Sessions, die laenger als ``behalte_tage`` abgelaufen sind (Aufraeumen;
    keine Buchungsdaten). Rueckgabe: Anzahl geloeschter Zeilen."""
    grenze = _jetzt() - timedelta(days=behalte_tage)
    alte = db.execute(select(AuthSession).where(AuthSession.expires_at < grenze)).scalars().all()
    for zeile in alte:
        db.delete(zeile)
    if alte:
        db.commit()
    return len(alte)


__all__ = [
    "AuthSession", "COOKIE_NAME", "SESSION_TTL", "RENEWAL_INTERVALL", "create_session",
    "get_session", "revoke_session", "purge_abgelaufen", "sid_hash",
]
