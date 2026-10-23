#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Preserve saved control placement when regenerating a local cloud profile."""

import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def saved_pool(directory):
    variables = directory / "terraform.tfvars"
    config = directory / "config.env"
    if variables.is_file():
        # Terraform handles valid HCL syntax and the legacy default for an
        # omitted input. Never source the profile or expose unrelated inputs.
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "main.tf").write_text(
                'variable "control_node_pool" {\n type=any\n default={enabled=false}\n nullable=false\n}\n'
            )
            result = subprocess.run(
                ["terraform", "console", "-no-color", "-var-file=" + str(variables.resolve())],
                cwd=temporary, input="jsonencode(var.control_node_pool)\n",
                text=True, capture_output=True, check=True,
            )
        for line in reversed(result.stdout.splitlines()):
            try:
                value = json.loads(json.loads(line))
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                pass
        raise ValueError("cannot read saved control_node_pool")
    if config.is_file():
        value = re.search(r"(?m)^CONTROL_NODE_POOL_B64=([A-Za-z0-9+/=]+)$", config.read_text())
        if value:
            return json.loads(base64.b64decode(value[1], validate=True))
    # Existing state/backend metadata also makes this an old profile.
    return {"enabled": not any(directory.iterdir())}


def main():
    directory = Path(sys.argv[1])
    value = saved_pool(directory)
    if not isinstance(value, dict):
        raise ValueError("control_node_pool must be an object")
    if "enabled" in value:
        enabled = value["enabled"]
        if enabled is None:
            value["enabled"] = False
        elif isinstance(enabled, str) and enabled in ("true", "false", "0", "1"):
            value["enabled"] = enabled in ("true", "1")
        elif not isinstance(enabled, bool):
            raise ValueError("control_node_pool.enabled must be a boolean")
    if len(sys.argv) > 2 and sys.argv[2]:
        value["enabled"] = sys.argv[2] == "true"
    if value.get("enabled") and value.get("node_selector"):
        raise ValueError("clear the existing-pool selector before enabling control-pool creation")
    print(json.dumps(value, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError):
        sys.exit("cannot preserve control-pool configuration; existing profile was not overwritten")
