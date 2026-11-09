# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Apply the standalone PVM switch to a validated sandboxd configuration."""

import argparse
from pathlib import Path
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        raise SystemExit("PVM configuration requires Python 3.11+ or tomli on Python 3.10") from None


MEMORY_TABLE = re.compile(
    r"(?ms)^[\t ]*\[plugin\.resource\.memory_limit\][\t ]*(?:#[^\n]*)?\n"
    r"(?P<body>.*?)(?=^[\t ]*\[|\Z)"
)
ENABLED = re.compile(
    r"""(?m)^([\t ]*)(?:enabled|"enabled"|'enabled')[\t ]*=[\t ]*"""
    r"(?:true|false)(?=[\t ]*(?:#|$))"
)


def configure(source: str, enabled: bool) -> str:
    config = tomllib.loads(source)
    table = config
    for key in ("plugin", "resource", "memory_limit"):
        # Match the sandboxd decoder's acceptance of uppercase struct aliases.
        if key.upper() in table:
            raise ValueError("PVM memory policy requires lowercase plugin.resource.memory_limit keys")
        table = table.get(key, {})
        if not isinstance(table, dict):
            raise ValueError(f"PVM memory policy requires {key} to be a TOML table")

    if "enabled" not in table:
        if enabled:
            raise ValueError("PVM memory policy requires a boolean enabled field when opting in")
        return source
    current = table["enabled"]
    if not isinstance(current, bool):
        raise ValueError("PVM memory policy requires a boolean enabled field")
    if current == enabled:
        return source

    # Verify the full parsed result so a lookalike line in a string cannot
    # satisfy the override or change unrelated fields.
    table["enabled"] = enabled
    for match in MEMORY_TABLE.finditer(source):
        body = ENABLED.sub(r"\1enabled = " + str(enabled).lower(), match["body"])
        rendered = source[:match.start("body")] + body + source[match.end("body"):]
        if tomllib.loads(rendered) == config:
            return rendered
    raise ValueError(
        "Use [plugin.resource.memory_limit] with a boolean enabled field "
        "to override the PVM memory policy"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("enabled", choices=("true", "false"))
    args = parser.parse_args()
    try:
        sys.stdout.write(configure(args.config.read_text(), args.enabled == "true"))
    except tomllib.TOMLDecodeError:
        parser.exit(1, "PVM sandboxd configuration must be valid TOML with no duplicate fields\n")
    except ValueError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
