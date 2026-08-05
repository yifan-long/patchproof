"""Small validation domain used for offline harness smoke tests."""

import re
from dataclasses import dataclass

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Registration:
    name: str
    email: str


def validate_registration(name: str, email: str) -> Registration:
    if not name.strip():
        raise ValueError("name is required")
    if not EMAIL.fullmatch(email.strip()):
        raise ValueError("email is invalid")
    # Intentional fixture bug: normalization must lowercase the address.
    return Registration(name=name.strip(), email=email.strip())
