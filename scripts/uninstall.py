"""Safely remove only media components owned by one explicit managed root."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "core"


class UninstallArgumentError(RuntimeError):
    """Raised so JSON callers receive argparse failures on stdout."""


class UninstallArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UninstallArgumentError(message)


def _write_json_stdout(payload: object) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def uninstall(managed_root: Path) -> dict[str, object]:
    component_error, uninstall_managed_components = _component_api()
    try:
        result = uninstall_managed_components(managed_root)
    except component_error as error:
        raise RuntimeError(str(error)) from error
    payload = result.to_dict()
    payload["schema_version"] = 1
    payload["managed_root"] = str(managed_root.resolve(strict=False))
    payload["ok"] = True
    return payload


def _component_api() -> tuple[type[Exception], Any]:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut.adapters.component_environment import (
        ComponentError,
        uninstall_managed_components,
    )
    return ComponentError, uninstall_managed_components


def main() -> None:
    parser = UninstallArgumentParser()
    parser.add_argument("--managed-root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    json_output = "--json" in sys.argv[1:]
    try:
        args = parser.parse_args()
        try:
            result = uninstall(args.managed_root)
        except RuntimeError as error:
            result = {
                "schema_version": 1,
                "ok": False,
                "error": {"code": "uninstall_failed", "message": str(error)},
            }
            exit_code = 1
        else:
            exit_code = 0
    except UninstallArgumentError as error:
        if not json_output:
            parser.print_usage(sys.stderr)
            parser.exit(2, f"{parser.prog}: error: {error}\n")
        result = {
            "schema_version": 1,
            "ok": False,
            "error": {"code": "uninstall_failed", "message": str(error)},
        }
        exit_code = 1
    if json_output:
        _write_json_stdout(result)
    else:
        print(result)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
