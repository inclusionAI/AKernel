"""Reject corrupt, unsafe and incorrectly attributed control release inputs."""
import hashlib
import errno
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('control_release', Path(__file__).with_name('install-control-release.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
COMMIT = 'a' * 40

class ControlReleaseTests(unittest.TestCase):
    def test_flattened_base_is_retained_without_changing_its_manifest_or_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            current = prefix / 'current';current.mkdir()
            manifest = json.dumps({'commit': COMMIT, 'files': {'pruned-runtime': 'old-hash'}})
            (current/'manifest.json').write_text(manifest)
            (current/'old-binary').write_bytes(b'old-bytes')
            with mock.patch('os.rename', side_effect=OSError(errno.EXDEV, 'image lower layer')):
                module.preserve_flattened_base(prefix)
            self.assertTrue(current.is_symlink())
            retained = prefix/'akernel-base'/COMMIT
            self.assertEqual((retained/'manifest.json').read_text(), manifest)
            self.assertEqual((retained/'old-binary').read_bytes(), b'old-bytes')
            self.assertEqual(current.resolve(), retained)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'release.tar.gz'
        self.output = self.root / 'output'
        self.output.mkdir()
        self.manifest = {'commit': COMMIT, 'dirty': False, 'profile': 'release',
                         'target': 'x86_64-unknown-linux-gnu', 'files': dict.fromkeys([
                             'install.sh', 'lib/package_manifest.py', 'runtime/adx-execd',
                             'runtime/adx-runtime-rootfs.img', 'runtime/envd/manifest.json'], 'b' * 64)}

    def archive_input(self, extra=None):
        with tarfile.open(self.archive, 'w:gz') as package:
            data = json.dumps(self.manifest).encode()
            member = tarfile.TarInfo('manifest.json');member.size = len(data)
            package.addfile(member, io.BytesIO(data))
            if extra:
                package.addfile(extra)
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_matching_source_and_complete_contract(self):
        sha = self.archive_input()
        self.assertEqual(module.unpack(self.archive, sha, COMMIT, self.output)['commit'], COMMIT)

    def test_checksum_corruption_is_rejected_before_extract(self):
        self.archive_input()
        with self.assertRaises(ValueError):module.unpack(self.archive, '0' * 64, COMMIT, self.output)
        self.assertFalse((self.output/'manifest.json').exists())

    def test_traversal_symlink_and_duplicate_are_rejected(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE),
                           ('manifest.json', tarfile.REGTYPE)]:
            with self.subTest(name=name):
                extra = tarfile.TarInfo(name);extra.type = kind;extra.linkname = '/tmp'
                sha = self.archive_input(extra)
                with self.assertRaises(ValueError):module.unpack(self.archive, sha, COMMIT, self.output)

    def test_source_target_dirty_and_missing_envd_are_rejected(self):
        for field, value in [('commit', 'c' * 40), ('dirty', True), ('profile', 'debug'),
                             ('target', 'aarch64-unknown-linux-gnu'), ('files', {})]:
            with self.subTest(field=field):
                old = self.manifest[field];self.manifest[field] = value
                sha = self.archive_input()
                with self.assertRaises(ValueError):module.unpack(self.archive, sha, COMMIT, self.output)
                self.manifest[field] = old

if __name__ == '__main__':unittest.main()
