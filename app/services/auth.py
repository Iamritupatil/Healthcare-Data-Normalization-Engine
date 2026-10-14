"""API keys.

Format: mvk_<prefix>_<secret>. The prefix is public: it identifies the key (safe to show
and log) and is how the key is looked up. The full key is verified against an
HMAC-SHA256 keyed with a server-side pepper, compared in constant time. The plain key is
shown once, at creation, and never stored.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

Role = Literal["admin", "ingest", "reader"]
ROLES: tuple[Role, ...] = ("admin", "ingest", "reader")

_KEY_FORMAT = re.compile(r"^mvk_([0-9a-f]{12})_[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class Principal:
    api_key_id: UUID
    org_id: UUID
    role: str


@dataclass(frozen=True)
class NewApiKey:
    key: str
    prefix: str
    key_hash: str


def hash_api_key(key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def generate_api_key(pepper: str) -> NewApiKey:
    prefix = secrets.token_hex(6)
    key = f"mvk_{prefix}_{secrets.token_urlsafe(32)}"
    return NewApiKey(key=key, prefix=prefix, key_hash=hash_api_key(key, pepper))


def authenticate(session: Session, key: str, pepper: str) -> Principal | None:
    match = _KEY_FORMAT.fullmatch(key)
    if match is None:
        return None
    row = session.execute(
        text("SELECT api_key_id, org_id, role, key_hash FROM medvora_find_api_key(:prefix)"),
        {"prefix": match.group(1)},
    ).one_or_none()
    if row is None or not hmac.compare_digest(row.key_hash, hash_api_key(key, pepper)):
        return None
    return Principal(api_key_id=row.api_key_id, org_id=row.org_id, role=row.role)
