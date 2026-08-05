from dataclasses import dataclass


@dataclass
class Job:
    state: str = "pending"
    applied_keys: set[str] | None = None

    def __post_init__(self) -> None:
        self.applied_keys = self.applied_keys or set()

    def transition(self, next_state: str, request_key: str) -> bool:
        if request_key in self.applied_keys:
            return False
        if (self.state, next_state) not in {("pending", "running"), ("running", "done")}:
            raise ValueError(f"invalid transition: {self.state}->{next_state}")
        self.state = next_state
        # Intentional fixture bug: the request key is not persisted.
        return True
