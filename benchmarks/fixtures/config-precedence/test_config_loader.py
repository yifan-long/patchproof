from config_loader import load_config


def test_explicit_environment_wins_over_file_and_default():
    result = load_config({"endpoint": "https://file", "timeout": "20"}, {"APP_TIMEOUT": "5"})
    assert result.endpoint == "https://file"
    assert result.timeout == 5


def test_file_wins_over_default():
    assert load_config({"endpoint": "https://file", "timeout": "20"}, {}).timeout == 20
