#!/usr/bin/env python3
"""Verify and install a complete ADX package without replacing worker rootfs."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unpack(archive, expected_sha, expected_commit, destination):
    if not re.fullmatch('[a-f0-9]{64}', expected_sha) or digest(archive) != expected_sha:
        raise ValueError('ADX archive checksum mismatch')
    if not re.fullmatch('[a-f0-9]{40}', expected_commit):
        raise ValueError('a full ADX source commit is required')
    with tarfile.open(archive, 'r:gz') as package:
        names = set()
        for member in package.getmembers():
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or
                    not (member.isdir() or member.isfile()) or str(name) in names):
                raise ValueError('unsafe or duplicate release archive member')
            names.add(str(name))
        package.extractall(destination, filter='data')
    manifest = json.loads((destination / 'manifest.json').read_text())
    if (manifest.get('commit') != expected_commit or manifest.get('dirty') is not False
            or manifest.get('profile') != 'release'
            or manifest.get('target') != 'x86_64-unknown-linux-gnu'):
        raise ValueError('ADX package must be a clean matching amd64 release')
    # The package's own verifier additionally checks ELF architecture, all file
    # hashes, Redis, matching Execd/rootfs and envd provenance on installation.
    for required in ['install.sh', 'lib/package_manifest.py', 'runtime/adx-execd',
                     'runtime/adx-runtime-rootfs.img', 'runtime/envd/manifest.json']:
        if required not in manifest['files']:
            raise ValueError('a complete envd-enabled ADX release is required')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    runtime = Path('/opt/akernel/runtime/akernel-runtime-rootfs.img')
    original = digest(runtime)
    with tempfile.TemporaryDirectory(prefix='adx-control-release-') as temporary:
        directory = Path(temporary)
        unpack(args.archive, args.sha256, args.commit, directory)
        subprocess.run(['bash', str(directory / 'install.sh'), '--prefix', '/opt/adx',
                        '--bin-dir', '/usr/local/bin'], check=True)
    if digest(runtime) != original:
        raise RuntimeError('the qualified execution runtime changed')


if __name__ == '__main__':
    main()
