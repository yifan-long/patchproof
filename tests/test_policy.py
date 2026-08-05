from patchproof.policy import classify_command


def test_pytest_is_safe():
    decision = classify_command("python -m pytest -q")
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_similar_command_name_is_not_safe():
    decision = classify_command("pytestevil -q")
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_delete_requires_approval():
    decision = classify_command("Remove-Item -Recurse build")
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_shell_composition_requires_approval():
    decision = classify_command("pytest -q && git push")
    assert decision.requires_approval is True
