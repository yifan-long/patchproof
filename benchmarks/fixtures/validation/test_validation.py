import pytest
from validation import validate_registration


def test_registration_normalizes_valid_values():
    assert validate_registration(" Ada ", "ADA@EXAMPLE.COM").email == "ada@example.com"


@pytest.mark.parametrize("name,email", [("", "a@example.com"), ("Ada", "invalid")])
def test_registration_rejects_invalid_values(name, email):
    with pytest.raises(ValueError):
        validate_registration(name, email)
