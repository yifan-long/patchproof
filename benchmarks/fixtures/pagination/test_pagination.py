import pytest
from pagination import has_next, page


def test_last_full_page_has_no_next_page():
    values = [1, 2, 3, 4]
    assert page(values, offset=2, limit=2) == [3, 4]
    assert has_next(values, offset=2, limit=2) is False


def test_partial_page_has_next_page():
    values = [1, 2, 3, 4, 5]
    assert page(values, offset=2, limit=2) == [3, 4]
    assert has_next(values, offset=2, limit=2) is True


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0)])
def test_pagination_rejects_invalid_bounds(offset, limit):
    with pytest.raises(ValueError):
        page([], offset=offset, limit=limit)
