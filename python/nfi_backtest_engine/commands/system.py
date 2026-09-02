"""Host inspection and execution-profile command orchestration."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import __version__
from ..canonical import write_json
from ..doctor import run_doctor
from ..errors import NfiBacktestError
from ..hardware import (
    GIB,
    create_execution_profile,
    inspect_hardware,
    load_execution_profile,
)

COMMAND_NAMES = frozenset({"doctor", "system", "help"})


def execute(
    args: argparse.Namespace,
    *,
    create_profile: Callable[..., dict[str, Any]] = create_execution_profile,
) -> int:
    """Execute health, hardware, Docker, or execution-profile commands."""
    if args.command_name == "doctor":
        fixes: list[dict[str, str]] = []
        if args.fix:
            managed_root = Path(".nfi")
            managed_root.mkdir(parents=True, exist_ok=True)
            fixes.append(
                {
                    "code": "MANAGED_ROOT_READY",
                    "status": "applied",
                    "detail": str(managed_root.resolve()),
                }
            )
        report = run_doctor(profile_path=args.profile)
        report["safe_fixes"] = fixes
        if args.output:
            write_json(args.output, report)
        if args.export_diagnostics:
            bundle = {
                "schema_version": "1.0.0",
                "privacy": {
                    "remote_transmission": False,
                    "environment_variables_included": False,
                    "credentials_included": False,
                },
                "product": {"name": "NFI Backtest Engine", "version": __version__},
                "runtime": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "executable_name": Path(sys.executable).name,
                },
                "doctor": report,
            }
            write_json(args.export_diagnostics, bundle)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(
                f"doctor: {'healthy' if report['healthy'] else 'unhealthy'}; "
                + ", ".join(
                    f"{check['name']}={check['status']}" for check in report["checks"]
                )
            )
            for check in report["checks"]:
                if check["status"] != "ok":
                    print(
                        f"  {check['code']}: {check['detail']}\n"
                        f"    Next: {check['remediation']}"
                    )
            if args.export_diagnostics:
                print(f"diagnostics: {args.export_diagnostics}")
        return 0 if report["healthy"] else 1

    if args.command_name != "system":
        raise AssertionError(f"unhandled system command: {args.command_name}")

    if args.system_command == "inspect":
        hardware = inspect_hardware()
        if args.output:
            write_json(args.output, hardware)
        print(json.dumps(hardware, ensure_ascii=False, indent=2))
        return 0
    if args.system_command == "docker":
        from ..docker_resources import (
            derive_docker_policy,
            inspect_docker_daemon,
        )
        from ..docker_runtime import (
            cleanup_stopped_managed_containers,
            list_managed_containers,
        )
        from ..reference_runtime import ensure_docker_config

        docker_config = ensure_docker_config()
        cleaned = (
            cleanup_stopped_managed_containers(docker_config=docker_config)
            if args.cleanup_stopped
            else []
        )
        daemon = inspect_docker_daemon(docker_config=docker_config)
        report = {
            "schema_version": "1.0.0",
            "daemon": daemon,
            "policy": derive_docker_policy(daemon),
            "managed_containers": list_managed_containers(docker_config=docker_config),
            "cleaned_stopped_containers": cleaned,
        }
        if args.output:
            write_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.system_command == "tune":
        if args.memory_cap_gib is not None and args.memory_cap_gib <= 0:
            raise NfiBacktestError("--memory-cap-gib must be positive")
        if args.output.exists() and not args.force:
            raise NfiBacktestError(
                f"execution profile already exists: {args.output}; "
                "use --force to recalibrate"
            )
        profile = create_profile(
            args.output,
            memory_cap_bytes=(
                int(args.memory_cap_gib * GIB) if args.memory_cap_gib is not None else None
            ),
            spool_directory=args.spool_directory,
        )
        limits = profile["limits"]
        print(
            f"execution profile -> {args.output}; "
            f"cpu_process_limit={limits['cpu_process_limit']}, "
            f"memory_cap={limits['memory_cap_bytes']}; "
            "workload process counts are measured on the first run"
        )
        return 0
    profile = load_execution_profile(args.profile)
    print(json.dumps(profile, ensure_ascii=False, indent=2))
    return 0
