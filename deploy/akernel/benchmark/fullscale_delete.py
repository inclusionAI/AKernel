#!/usr/bin/env python3
"""Delete every sandbox recorded by fullscale_create.py."""

from concurrent.futures import ThreadPoolExecutor

import yr_sandbox


with open("/state/placements", encoding="utf-8") as source:
    sandbox_ids = [line.split(maxsplit=1)[1].strip() for line in source if line.strip()]
with ThreadPoolExecutor(max_workers=8) as executor:
    results = list(executor.map(yr_sandbox.Sandbox.delete, sandbox_ids))
print(f"deleted_total={len(results)}")
