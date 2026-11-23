"""Minimaler Stub-OIDC-IdP für lokale End-to-End-Tests des Server-Modus.

Stellt Discovery, JWKS, authorize und token bereit. Einziger Nutzer:
caro@example.org (Allowlist-Test) — jede andere Mail wird vom echten
RechnungsFee-Allowlist-Mechanismus abgelehnt.

Start:  python3 stub_idp.py   (läuft auf 127.0.0.1:9999)
"""
import base64
import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwcrypto import jwk  # falls nicht vorhanden: siehe requirements-Hinweis unten

ISSUER = "http://localhost:9999"
CLIENT_ID = "rechnungsfee-test"
REDIRECT = "http://127.0.0.1:8000/api/auth/callback"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_jwk = jwk.JWK.from_pem(_priv_pem)
_jwk_dict = json.loads(_jwk.export_public())
_jwk_dict.update({"kid": "test-key-1", "alg": "RS256", "use": "sig"})

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def make_id_token(email: str, nonce: str) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "kid": "test-key-1", "typ": "JWT"}
    payload = {
        "iss": ISSUER, "sub": "user-" + email, "aud": CLIENT_ID,
        "exp": now + 600, "iat": now, "email": email, "email_verified": True,
        "nonce": nonce,
    }
    from jwcrypto import jws, jwt as jwcrypto_jwt
    tok = jwcrypto_jwt.JWT(header=header, claims=payload)
    tok.make_signed_token(_jwk)  # jwcrypto erwartet ein JWK-Objekt, keinen cryptography-Key
    return tok.serialize()

# single-use codes
_codes: dict[str, dict] = {}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # ruhig
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/.well-known/openid-configuration":
            self._json({
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/jwks",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile"],
                "claims_supported": ["sub", "email", "email_verified"],
                "code_challenge_methods_supported": ["S256"],
            })
        elif u.path == "/jwks":
            self._json({"keys": [_jwk_dict]})
        elif u.path == "/authorize":
            q = parse_qs(u.query)
            assert q["client_id"][0] == CLIENT_ID
            assert q["redirect_uri"][0] == REDIRECT
            assert q["code_challenge_method"][0] == "S256"
            code = _b64u(hashlib.sha256(str(time.time_ns()).encode()).digest())
            _codes[code] = {"nonce": q.get("nonce", [None])[0], "challenge": q["code_challenge"][0]}
            # Auto-Approve: direkt zurück zum Redirect mit code+state
            sep = "&" if "?" in REDIRECT else "?"
            try:
                self.send_response(302)
                self.send_header("Location", f"{REDIRECT}{sep}code={code}&state={q['state'][0]}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass  # Client hat Verbindung beendet — harmlos
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            u = urlparse(self.path)
            if u.path == "/token":
                n = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(n).decode())
                code = form["code"][0]
                entry = _codes.pop(code, None)
                if entry is None:
                    self._json({"error": "invalid_grant"}, 400)
                    return
                # PKCE verifizieren
                verifier = form["code_verifier"][0]
                if _b64u(hashlib.sha256(verifier.encode()).digest()) != entry["challenge"]:
                    self._json({"error": "invalid_grant", "error_description": "PKCE mismatch"}, 400)
                    return
                self._json({
                    "access_token": "stub-access",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "id_token": make_id_token("caro@example.org", entry["nonce"] or ""),
                })
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # Stub-IdP: Fehler als JSON, nie crashen
            try:
                self._json({"error": "server_error", "error_description": repr(exc)[:200]}, 500)
            except Exception:
                pass

if __name__ == "__main__":
    print(f"Stub-IdP auf {ISSUER} — Client {CLIENT_ID}, Redirect {REDIRECT}")
    ThreadingHTTPServer(("127.0.0.1", 9999), H).serve_forever()
