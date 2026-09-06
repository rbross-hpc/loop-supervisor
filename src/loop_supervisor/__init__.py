"""loop_supervisor: headless planner/architect/builder/auditor loop over OpenCode."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("loop-supervisor")
except PackageNotFoundError:
    # Running from a source checkout with no installed distribution
    # metadata (e.g. a bare `sys.path` import, not `pip install`/`pip
    # install -e`). Importing the package must not fail just because
    # `--version`/`doctor` cannot resolve a real version in that case.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
