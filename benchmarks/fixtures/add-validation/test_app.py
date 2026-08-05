import pytest
from app import add


def test_add_positive_values():
    assert add(1, 2) == 3


def test_add_rejects_negative_values():
    with pytest.raises(ValueError, match="negative input"):
        add(-1, 2)
