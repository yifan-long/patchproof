import pytest
from state import Job


def test_replaying_request_key_is_a_noop():
    job = Job()
    assert job.transition("running", "request-1") is True
    assert job.transition("running", "request-1") is False
    assert job.state == "running"


def test_invalid_state_transition_is_rejected():
    with pytest.raises(ValueError):
        Job().transition("done", "request-1")
