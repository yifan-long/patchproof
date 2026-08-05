# Configuration precedence fixture

The loader combines defaults, a file, and explicit environment overrides while
keeping the source representation deterministic for tests. Timeout precedence
is intentionally reversed so the required check fails before repair.
