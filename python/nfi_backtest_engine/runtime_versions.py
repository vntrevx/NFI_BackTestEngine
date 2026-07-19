"""Runtime dependency identities that can change strategy vector output."""

from __future__ import annotations

import platform
from importlib.metadata import version


def vector_dependency_versions() -> dict[str, str]:
    """Return the exact dataframe stack included in vector cache identities."""
    return {
        "python": platform.python_version(),
        "numpy": version("numpy"),
        "pandas": version("pandas"),
        "pyarrow": version("pyarrow"),
        "ta_lib": version("TA-Lib"),
    }
