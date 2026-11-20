#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Render opt-in sandboxd controls without changing their default semantics."""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import tempfile


CONTROLS = (
    ("AKERNEL_FIRECRACKER_CHECKPOINT_CONCURRENCY", "plugin.resource", "firecracker_checkpoint_concurrency"),
    ("AKERNEL_FIRECRACKER_RESTORE_CONCURRENCY", "plugin.resource", "firecracker_restore_concurrency"),
    ("AKERNEL_RUNSC_CLI_GOMAXPROCS", "plugin.runtime.runsc", "cli_gomaxprocs"),
)
MARKERS = {
    "plugin.resource": "# AKERNEL_RUNTIME_RESOURCE_TUNING",
    "plugin.runtime.runsc": "# AKERNEL_RUNTIME_RUNSC_TUNING",
}


def render(text, environ):
    fields = {section: {} for section in MARKERS}
    for name, section, key in CONTROLS:
        value = environ.get(name, "")
        if not value:
            continue
        if not re.fullmatch(r"0|[1-9][0-9]{0,3}", value) or int(value) > 1024:
            raise ValueError(f"{name} must be an integer between 0 and 1024")
        fields[section][key] = str(int(value))
    memory_name = "AKERNEL_FIRECRACKER_CHECKPOINT_MEMORY_MAX_BYTES"
    memory = environ.get(memory_name, "")
    if memory:
        if not re.fullmatch(r"0|[1-9][0-9]{0,18}", memory) or int(memory) > 9223372036854775807:
            raise ValueError(f"{memory_name} must be an integer between 0 and 9223372036854775807 bytes")
        fields["plugin.resource"]["firecracker_checkpoint_memory_max_bytes"] = str(int(memory))
    raw_args = environ.get("AKERNEL_RUNSC_EXTRA_ARGS", "")
    if raw_args:
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as error:
            raise ValueError("AKERNEL_RUNSC_EXTRA_ARGS must be a JSON array of strings") from error
        if not isinstance(args, list) or not all(
            isinstance(arg, str) and "\0" not in arg
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in arg)
            for arg in args
        ):
            raise ValueError("AKERNEL_RUNSC_EXTRA_ARGS must be a JSON array of strings without NUL")
        fields["plugin.runtime.runsc"]["extra_args"] = json.dumps(args, ensure_ascii=False).replace("\x7f", "\\u007f")

    lines = text.splitlines(keepends=True)
    for section, values in fields.items():
        if not values:
            continue
        marker = MARKERS[section]
        current = None
        positions = []
        for index, line in enumerate(lines):
            header = re.fullmatch(r"\s*\[([^\[\]]+)\]\s*(?:#.*)?", line.strip())
            if header:
                current = header.group(1)
            if line.strip() == marker:
                positions.append(index)
                if current != section:
                    raise ValueError(f"{marker} must be under [{section}]")
            if current == section:
                for key in values:
                    if re.match(r"\s*" + re.escape(key) + r"\s*=", line):
                        raise ValueError(f"{key} is already set under [{section}]; use the template or the environment override")
        if len(positions) != 1:
            raise ValueError(f"runtime tuning requires exactly one {marker} under [{section}]")
        index = positions[0]
        indent = lines[index][:len(lines[index]) - len(lines[index].lstrip())]
        lines[index] = "".join(f"{indent}{key}={value}\n" for key, value in values.items())
    return "".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--check", action="store_true", help="validate without writing")
    args = parser.parse_args()
    try:
        original = args.path.read_text()
        rendered = render(original, os.environ)
        if args.check or rendered == original:
            return
        descriptor, temporary = tempfile.mkstemp(prefix=args.path.name + ".", dir=args.path.parent)
        try:
            os.fchmod(descriptor, stat.S_IMODE(args.path.stat().st_mode))
            with os.fdopen(descriptor, "w") as output:
                descriptor = None
                output.write(rendered)
            os.replace(temporary, args.path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, ValueError) as error:
        parser.exit(2, f"runtime tuning: {error}\n")


if __name__ == "__main__":
    main()
