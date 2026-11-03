#!/usr/bin/env python3
"""Build an API/Ingress-only image from an immutable base and full ADX archive."""

import argparse
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', args.base):
        parser.error('--base must be an immutable image digest')
    if not re.fullmatch('[a-f0-9]{40}', args.commit):
        parser.error('--commit must be the full source revision')
    checksum = hashlib.sha256()
    with args.archive.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    actual = checksum.hexdigest()
    if not re.fullmatch('[a-f0-9]{64}', args.sha256) or actual != args.sha256:
        parser.error('archive SHA256 mismatch')
    root = Path(__file__).resolve().parents[2]
    # Only the explicit public package and build scripts enter Docker's context.
    with tempfile.TemporaryDirectory(prefix='akernel-control-build-') as temporary:
        context = Path(temporary)
        shutil.copyfile(args.archive, context / 'release.tar.gz')
        shutil.copyfile(root / 'builder/control.Dockerfile', context / 'Dockerfile')
        shutil.copyfile(root / 'builder/scripts/install-control-release.py',
                        context / 'install-control-release.py')
        subprocess.run(['docker', 'build', '--platform', 'linux/amd64',
                        '--build-arg', 'AKERNEL_BASE_IMAGE=' + args.base,
                        '--build-arg', 'ADX_RELEASE_SHA256=' + args.sha256,
                        '--build-arg', 'ADX_RELEASE_COMMIT=' + args.commit,
                        '--tag', args.tag, str(context)], check=True)


if __name__ == '__main__':
    main()
