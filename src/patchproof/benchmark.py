"""Compatibility exports for the evaluation benchmark CLI."""

from . import evaluation as _evaluation_compat
from .evals import benchmark as _implementation
from .evals.benchmark import *  # noqa: F403, I001
from .evals.benchmark import Settings, main  # noqa: F401


def _run_cli(args):
    """Forward the historical CLI hook while preserving monkeypatching."""

    _implementation.Settings = Settings
    _implementation.EvaluationOrchestrator = _evaluation_compat.EvaluationOrchestrator
    return _implementation._run_cli(args)


if __name__ == "__main__":
    main()
