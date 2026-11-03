"""Fail-closed disk checks, safe preparation and file ownership preservation."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare-application-logs.py'
spec = importlib.util.spec_from_file_location('application_logs', SCRIPT)
logs = importlib.util.module_from_spec(spec);spec.loader.exec_module(logs)


class ApplicationLogsTest(unittest.TestCase):
    def test_system_disk_or_wrong_view_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(ValueError):logs.verify_disk('/home/akernel', path, '')
            dev = os.stat(path).st_dev
            mount = f'42 24 {os.major(dev)}:{os.minor(dev)} / /home/akernel rw - ext4 /dev/test rw'
            with self.assertRaises(ValueError):logs.verify_disk('/home/akernel', path, mount)
            mount = '42 24 999:999 / /home/akernel rw - xfs /dev/test rw'
            with self.assertRaises(ValueError):logs.verify_disk('/home/akernel', path, mount)
            self.assertEqual(list(path.iterdir()), [])

    @unittest.skipUnless(os.geteuid() == 0, 'ownership fixture requires root')
    def test_repeated_preparation_never_changes_business_files(self):
        with tempfile.TemporaryDirectory() as directory:
            logs.prepare_directory(directory, 'shared/app-logs', 0, 1000, 0o2770)
            leaf = Path(directory) / 'shared/app-logs';file = leaf / 'business.log'
            file.write_text('original');file.chmod(0o600);before = file.stat()
            logs.prepare_directory(directory, 'shared/app-logs', 0, 1000, 0o2770)
            after = file.stat()
            self.assertEqual((before.st_ino, before.st_uid, before.st_gid, before.st_mode),
                             (after.st_ino, after.st_uid, after.st_gid, after.st_mode))
            self.assertEqual(file.read_text(), 'original')
            self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o2770)
            with self.assertRaises(ValueError):logs.prepare_directory(directory, 'shared/app-logs', 1000, 1000, 0o2770)
            self.assertEqual(file.stat().st_mode, before.st_mode)

    def test_symlink_and_world_writable_declarations_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'shared').symlink_to('/tmp')
            with self.assertRaises(OSError):logs.prepare_directory(directory, 'shared/app-logs', 0, 1000, 0o2770)
            for path in ['../logs', '/app/logs', 'adx/logs']:
                with self.assertRaises(ValueError):logs.prepare_directory(directory, path, 0, 1000, 0o2770)
            with self.assertRaises(ValueError):logs.prepare_directory(directory, 'logs', 0, 1000, 0o777)

    @unittest.skipUnless(os.geteuid() == 0, 'migration fixture requires root')
    def test_explicit_migration_preserves_inodes_and_legacy_points_to_one_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);source = root/'runtime-v4/agent-service-logs'
            source.mkdir(parents=True);source.chmod(0o700)
            file = source/'existing.log';file.write_text('preserve-me');file.chmod(0o600)
            before = file.stat()
            logs.migrate(directory, 'runtime-v4/agent-service-logs', 'shared/app-logs', 0, 1000, 0o2770)
            after = (root/'shared/app-logs/existing.log').stat()
            self.assertEqual((before.st_ino, before.st_uid, before.st_gid, before.st_mode),
                             (after.st_ino, after.st_uid, after.st_gid, after.st_mode))
            self.assertEqual(os.readlink(source), '/app/logs')
            logs.migrate(directory, 'runtime-v4/agent-service-logs', 'shared/app-logs', 0, 1000, 0o2770)
            self.assertEqual((root/'shared/app-logs/existing.log').read_text(), 'preserve-me')

    def test_boot_service_checks_disk_identity_and_only_binds_one_source(self):
        script = logs.boot_script('/home/akernel', 'shared/app-logs', 0, 1000, 0o2770)
        self.assertIn('findmnt -rn -M', script)
        self.assertIn('mount --bind "$log_source" /app/logs', script)
        self.assertNotIn('chmod -R', script)
        self.assertNotIn('chown -R', script)

    def test_initializer_enters_original_host_root_with_its_submounts(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(logs.subprocess, 'run') as run:
            logs.install(directory, '/host-proc', '/home/akernel', 'shared/app-logs', 0, 1000, 0o2770)
            command = run.call_args.args[0]
            self.assertIn('--root=/host-proc/1/root', command)
            self.assertIn('--wd=/host-proc/1/root', command)
            self.assertIn('--mount=/host-proc/1/ns/mnt', command)
            self.assertNotIn('--root=' + directory, command)
            self.assertFalse(any(arg.startswith('--pid') for arg in command),
                             'a Pod cannot enter its ancestor host PID namespace')
            self.assertNotIn('systemctl', ' '.join(command))
            enabled = Path(directory)/'etc/systemd/system/multi-user.target.wants/akernel-application-logs.service'
            self.assertEqual(os.readlink(enabled), '/etc/systemd/system/akernel-application-logs.service')


if __name__ == '__main__':unittest.main()
