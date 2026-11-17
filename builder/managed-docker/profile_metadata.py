"""Operator-only resource metadata derived from Sandboxd's profile geometry.

Merge the output into the ADXlet service's config.labels before starting the
worker. No credentials, Registry addresses or node paths enter scheduling.
"""

import argparse
import json
import re
import tomllib
from pathlib import Path

LABEL_PREFIX = "AKERNEL_RUNTIME_PROFILE_"
MAX_STORAGE_MB = (1 << 44) - 1


def resource_labels(config: dict) -> dict[str, str]:
    profiles = config.get("plugin", {}).get("managed_runtime", {}).get("profiles", {})
    labels = {}
    for name, profile in profiles.items():
        if re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name) is None:
            raise ValueError("Invalid managed runtime profile name")
        upper, root = profile.get("writable_mb"), profile.get("minimum_root_mb", 0)
        if isinstance(root, int) and not isinstance(root, bool) and root == 0:
            root = 4096
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (upper, root)):
            raise ValueError("Invalid managed runtime storage geometry")
        if upper + root > MAX_STORAGE_MB:
            raise ValueError("Managed runtime storage geometry overflows bytes")
        labels[LABEL_PREFIX + name] = str(upper + root)
    return labels


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sandboxd_config", type=Path)
    args = parser.parse_args()
    print(json.dumps(resource_labels(tomllib.loads(args.sandboxd_config.read_text()))))
