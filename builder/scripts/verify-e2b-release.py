#!/usr/bin/env python3
# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Reject an optional E2B runtime assembled with a different ADX release."""
import json
from pathlib import Path
import sys


def verify(component_path, release_path):
    component = json.loads(component_path.read_text())
    release = json.loads(release_path.read_text())
    if any(not component.get(key) or component[key] != release.get(key)
           for key in ("commit", "target")):
        raise ValueError("E2B runtime and ADX platform must come from the same build and architecture")
    if component["files"]["adx-execd"] != release.get("files", {}).get("runtime/adx-execd"):
        raise ValueError("E2B runtime and platform Execd checksums differ")


if __name__ == "__main__":
    verify(Path(sys.argv[1]), Path(sys.argv[2]))
