"""Local RSA keys, token minting and fake key fetchers for Firebase/App Check tests."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.auth_firebase import FetchedKeys

PROJECT = "demo-project"
PROJECT_NUMBER = "123456789012"
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class KeyPair:
    def __init__(self, kid: str) -> None:
        self.kid = kid
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self.private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.public_pem = self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def x509_pem(self) -> str:
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.private.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .sign(self.private, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM).decode()

    def jwk(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return jwk


class FakeFetcher:
    def __init__(self, keys: dict, max_age: int = 3600, fail: bool = False) -> None:
        self.keys = keys
        self.max_age = max_age
        self.fail = fail
        self.calls = 0

    def fetch(self) -> FetchedKeys:
        self.calls += 1
        if self.fail:
            raise RuntimeError("network down")
        return FetchedKeys(keys=dict(self.keys), max_age_seconds=self.max_age)


def firebase_claims(**over) -> dict:
    claims = {
        "aud": PROJECT,
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "sub": "uid-1",
        "iat": NOW - 10,
        "auth_time": NOW - 20,
        "exp": NOW + 3000,
        "firebase": {"sign_in_provider": "anonymous", "identities": {}},
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def appcheck_claims(**over) -> dict:
    claims = {
        "iss": f"https://firebaseappcheck.googleapis.com/{PROJECT_NUMBER}",
        "aud": [f"projects/{PROJECT_NUMBER}", f"projects/{PROJECT}"],
        "sub": "1:123456789012:web:abc",
        "iat": NOW - 10,
        "exp": NOW + 3000,
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def mint(pair: KeyPair, claims: dict, kid: str | None = None, typ: str = "JWT") -> str:
    return jwt.encode(
        claims,
        pair.private_pem,
        algorithm="RS256",
        headers={"kid": kid or pair.kid, "typ": typ},
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_hs256_with_public_key(pair: KeyPair, claims: dict) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": pair.kid}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = hmac.new(pair.public_pem, f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def mint_alg_none(claims: dict, kid: str) -> str:
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": kid}).encode())
    return f"{header}.{_b64(json.dumps(claims).encode())}."
