import json
from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    name: str
    active: bool = True


def decode_user(payload: str) -> User:
    raw = json.loads(payload)
    # Intentional fixture bug: v1 payloads omitted the active field.
    return User(name=str(raw["name"]), active=bool(raw["active"]))


def encode_user(user: User) -> str:
    return json.dumps({"name": user.name, "active": user.active}, sort_keys=True)
