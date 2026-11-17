"""Install one immutable runtime rootfs; verify both old and current Execd."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile


def digest(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Runtime artifact must be a regular file')
        result = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
        return result.hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pin(root, checksum, execd_checksum, *, source, execd, stage):
    if not all(re.fullmatch('[a-f0-9]{64}', value) for value in (checksum, execd_checksum)):
        raise ValueError('Runtime rootfs and Execd require SHA256 values')
    if digest(execd) != execd_checksum:
        raise ValueError('Pinned runtime Execd differs from this platform image')
    root = Path(root)
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError('Runtime cache must be a canonical mounted directory')
    directory = root / 'runtime-rootfs'
    if stage and not directory.exists():
        directory.mkdir(mode=0o700)
        sync_directory(root)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Runtime cache directory is missing or aliased')
    attributes = directory.stat()
    if attributes.st_uid != os.geteuid() or attributes.st_mode & 0o022:
        raise ValueError('Runtime cache must be owned and not writable by other users')
    if stage and digest(source) != checksum:
        raise ValueError('Runtime source SHA256 does not match the pin')
    if stage:
        sync_directory(root)
    destination = directory / (checksum + '.img')
    if destination.exists() or destination.is_symlink():
        if digest(destination) != checksum:
            raise ValueError('Existing immutable runtime rootfs is corrupted')
        if stage:
            sync_directory(directory)
        return destination
    if not stage:
        raise ValueError('Pinned runtime rootfs has not been staged')
    temporary = None
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        with os.fdopen(descriptor, 'rb') as input_stream:
            if not stat.S_ISREG(os.fstat(input_stream.fileno()).st_mode):
                raise ValueError('Runtime source must be a regular file')
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.stage-', delete=False) as output:
                temporary = Path(output.name)
                result = hashlib.sha256()
                for chunk in iter(lambda: input_stream.read(1024 * 1024), b''):
                    result.update(chunk)
                    output.write(chunk)
                if result.hexdigest() != checksum:
                    raise ValueError('Runtime source SHA256 does not match the pin')
                output.flush()
                os.fchmod(output.fileno(), 0o444)
                os.fsync(output.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            if digest(destination) != checksum:
                raise ValueError('Concurrent runtime rootfs installation differs') from None
        sync_directory(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['stage', 'verify'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--execd-sha256', required=True)
    args = parser.parse_args()
    pin(args.root, args.sha256, args.execd_sha256,
        source='/opt/akernel/runtime/akernel-runtime-rootfs.img',
        execd='/opt/adx/current/runtime/adx-execd', stage=args.mode == 'stage')
