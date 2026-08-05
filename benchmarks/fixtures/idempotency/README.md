# Idempotency and state-transition fixture

The fixture intentionally forgets to persist applied request keys, causing the
replay check to fail before repair.
