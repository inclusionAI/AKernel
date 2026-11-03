#!/usr/bin/env python3
"""Prepare a version-independent log directory only on a verified host data disk.

Only the privileged initializer sees the host root/proc. Runtime containers get
one data subdirectory. Existing application files are never chmod/chown'd.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess


def normalized(path):
    return isinstance(path, str) and path.startswith('/') and path != '/' and os.path.normpath(path) == path and not re.search(r'[\s\x00-\x1f]', path)


def verify_disk(disk_root, disk_view, mountinfo):
    if not normalized(disk_root):
        raise ValueError('data disk root must be an absolute normalized path')
    records = [line.split() for line in mountinfo.splitlines()]
    match = [r for r in records if len(r) > 8 and r[4] == disk_root]
    if len(match) != 1 or match[0][match[0].index('-') + 1] != 'xfs':
        raise ValueError('application logs require the exact host data root to be mounted XFS')
    device = os.stat(disk_view).st_dev
    if match[0][2] != f'{os.major(device)}:{os.minor(device)}':
        raise ValueError('initializer data view differs from the host data mount')


def prepare_directory(root, relative, uid, gid, mode):
    if (not relative or relative.startswith('/') or any(p in ('', '.', '..') for p in relative.split('/'))
            or not re.fullmatch(r'[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*', relative)):
        raise ValueError('log subdirectory must be a safe relative path')
    if relative.split('/')[0] in ('adx', 'sandboxd', 'images', 'filestore'):
        raise ValueError('application logs must not overlap runtime state')
    if not (0 <= uid < 2**32 - 1 and 0 <= gid < 2**32 - 1 and mode & 0o700 == 0o700 and not mode & 0o007):
        raise ValueError('invalid log ownership or permissions; world access is forbidden')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        parts = relative.split('/')
        for i, part in enumerate(parts):
            parent = os.fstat(fd)
            if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) & 0o022:
                raise ValueError('unsafe writable/non-root intermediate directory')
            created = False
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd);fd = child
            if i == len(parts) - 1:
                if created:
                    os.fchown(fd, uid, gid);os.fchmod(fd, mode)
                info = os.fstat(fd)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
                    raise ValueError('existing log directory ownership/mode differs; reconcile explicitly, files unchanged')
            elif created:
                os.fchmod(fd, 0o755)
    finally:
        os.close(fd)


def migrate(root, source_relative, relative, uid, gid, mode):
    """Explicit, one-time same-filesystem rename; retain the legacy name as one alias."""
    for path in (source_relative, relative):
        if not re.fullmatch(r'[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*', path):
            raise ValueError('migration paths must be safe relative directories')
    source, target = Path(root)/source_relative, Path(root)/relative
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('overlapping migration paths')
    receipt = Path(root)/'shared/application-logs-migration.json'
    if source.is_symlink():
        if (os.readlink(source) == '/app/logs' and target.is_dir() and receipt.exists()
                and json.loads(receipt.read_text())['inode'] == target.stat().st_ino):
            return
        raise ValueError('unknown pre-existing migration alias')
    if not source.is_dir() or target.exists() or target.is_symlink() or receipt.exists():
        raise ValueError('migration needs an existing source and absent destination/receipt')
    for parent in [source.parent, *source.parents]:
        if parent == Path(root).parent:break
        if parent.is_symlink():raise ValueError('symlinked migration parent')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if target.parent.is_symlink() or source.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError('migration must remain on the same verified data disk')
    info = source.stat()
    record = {'source':source_relative, 'destination':relative, 'inode':info.st_ino,
              'uid':info.st_uid, 'gid':info.st_gid, 'mode':stat.S_IMODE(info.st_mode),
              'phase':'prepared', 'alias':'/app/logs'}
    fd = os.open(receipt, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:json.dump(record, output)
    os.rename(source, target)
    # Only the directory root changes; existing files retain their owners/modes/xattrs.
    os.chown(target, uid, gid);os.chmod(target, mode)
    source.symlink_to('/app/logs', target_is_directory=True)
    record['phase'] = 'complete';receipt.write_text(json.dumps(record)+'\n')


def boot_script(disk_root, relative, uid, gid, mode):
    source = disk_root + '/' + relative
    return f'''#!/bin/sh
set -eu
log_disk={shlex.quote(disk_root)}
log_source={shlex.quote(source)}
[ "$(findmnt -rn -M "$log_disk" -o FSTYPE)" = xfs ]
[ -d "$log_source" ] && [ ! -L "$log_source" ]
[ "$(stat -c %d "$log_source")" = "$(stat -c %d "$log_disk")" ]
[ "$(stat -c %u:%g:%a "$log_source")" = "{uid}:{gid}:{mode:o}" ]
if mountpoint -q /app/logs; then
    [ "$(stat -c %d:%i /app/logs)" = "$(stat -c %d:%i "$log_source")" ]
else
    if [ -e /app/logs ]; then
        [ -d /app/logs ] && [ ! -L /app/logs ]
        [ -z "$(find /app/logs -mindepth 1 -maxdepth 1 -print -quit)" ]
    else
        mkdir -p /app/logs
    fi
    mount --bind "$log_source" /app/logs
fi
'''


def install(host_root, host_proc, disk_root, relative, uid, gid, mode):
    host = Path(host_root)
    # No user-controlled file contents or credentials are installed on the host.
    script = host/'usr/local/libexec/akernel-application-logs'
    unit = host/'etc/systemd/system/akernel-application-logs.service'
    for parent in (script.parent, unit.parent):
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink():
            raise ValueError('host service directory must not be a symlink')
    script.write_text(boot_script(disk_root, relative, uid, gid, mode));script.chmod(0o755)
    unit.write_text(f'''[Unit]
Description=Bind AKernel persistent application logs
RequiresMountsFor={disk_root}
After=local-fs.target
Before=kubelet.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/akernel-application-logs
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
''')
    unit.chmod(0o644)
    # Enable the next host boot without D-Bus: systemctl cannot authenticate a
    # peer in an ancestor PID namespace. Startup is checked directly below.
    wants = unit.parent/'multi-user.target.wants'
    wants.mkdir(exist_ok=True)
    if wants.is_symlink():
        raise ValueError('host boot target directory must not be a symlink')
    enabled = wants/unit.name
    target = '/etc/systemd/system/' + unit.name
    if enabled.is_symlink():
        if os.readlink(enabled) not in (target, '../' + unit.name):
            raise ValueError('unexpected existing application log boot link')
    elif enabled.exists():
        raise ValueError('application log boot link is not a symlink')
    else:
        enabled.symlink_to(target)
    # Enter PID1's original root, not the initializer's bind clone of it: the
    # clone may hide host submounts such as /proc after switching namespaces.
    # Entering an ancestor PID namespace is not permitted from a normal Pod.
    # The bind itself needs only the original host mount namespace and root.
    subprocess.run(['nsenter', f'--mount={host_proc}/1/ns/mnt',
                    f'--root={host_proc}/1/root', '--wd=/', '--',
                    '/usr/local/libexec/akernel-application-logs'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-root', required=True);parser.add_argument('--host-proc', required=True)
    parser.add_argument('--disk-root', required=True);parser.add_argument('--disk-view', required=True)
    parser.add_argument('--relative', required=True);parser.add_argument('--uid', required=True, type=int)
    parser.add_argument('--gid', required=True, type=int);parser.add_argument('--mode', required=True)
    parser.add_argument('--migrate-from', help='Explicit one-time migration while all writers are drained')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-7]{4}', args.mode):raise ValueError('mode must be four octal digits')
    verify_disk(args.disk_root, args.disk_view, (Path(args.host_proc)/'1/mountinfo').read_text())
    if args.migrate_from:
        migrate(args.disk_view, args.migrate_from, args.relative, args.uid, args.gid, int(args.mode, 8))
    prepare_directory(args.disk_view, args.relative, args.uid, args.gid, int(args.mode, 8))
    install(args.host_root, args.host_proc, args.disk_root, args.relative, args.uid, args.gid, int(args.mode, 8))
    print('Application log data disk, ownership, host bind and boot service verified')


if __name__ == '__main__':main()
