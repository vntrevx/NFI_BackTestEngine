"""Trusted subprocess environment for Docker CLI boundaries."""

from __future__ import annotations

import os


def docker_subprocess_environment() -> dict[str, str]:
    """Select Docker only through argv and the explicit isolated config.

    Docker endpoint, context, TLS, and configuration variables are deliberately
    not inherited. Callers pass ``--config`` and therefore use the local default
    daemon selected by that credential-free configuration. PATH is the sole
    POSIX lookup input retained so the already-resolved Docker CLI can execute
    its system helpers; Windows additionally requires its system root.
    """
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    return environment
