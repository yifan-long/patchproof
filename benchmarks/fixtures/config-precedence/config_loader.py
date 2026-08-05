from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    endpoint: str
    timeout: int


def load_config(values: dict[str, str], env: dict[str, str]) -> Config:
    endpoint = env.get("APP_ENDPOINT", values.get("endpoint", "http://localhost"))
    # Intentional fixture bug: explicit environment values must win.
    timeout = int(values.get("timeout", env.get("APP_TIMEOUT", "10")))
    return Config(endpoint=endpoint, timeout=timeout)
