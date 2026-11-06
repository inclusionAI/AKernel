"""Real filesystem checks for immutable runtime image installation."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pin-runtime-rootfs.py'
SPEC = importlib.util.spec_from_file_location('pin_runtime_rootfs', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PinRuntimeRootfsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'source.img'
        self.source.write_bytes(b'qualified runtime bytes')
        self.executable = self.root / 'adx-execd'
        self.executable.write_bytes(b'unchanged static executable')
        self.checksum = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.execd_checksum = hashlib.sha256(self.executable.read_bytes()).hexdigest()

    def run_pin(self, stage=True):
        return MODULE.pin(self.root, self.checksum, self.execd_checksum,
                          source=self.source, execd=self.executable, stage=stage)

    def test_stage_reuse_and_verify_keep_same_immutable_inode(self):
        cached = self.run_pin()
        inode = cached.stat().st_ino
        self.assertEqual(cached.read_bytes(), self.source.read_bytes())
        self.assertEqual(cached.stat().st_mode & 0o777, 0o444)
        self.run_pin()
        self.run_pin(stage=False)
        self.assertEqual(cached.stat().st_ino, inode)
        self.assertFalse(list(cached.parent.glob('.stage-*')))

    def test_changed_current_execd_is_rejected_even_with_good_cache(self):
        cached = self.run_pin()
        self.executable.write_bytes(b'new guest ABI')
        with self.assertRaisesRegex(ValueError, 'Execd differs'):
            self.run_pin(stage=False)
        self.assertEqual(hashlib.sha256(cached.read_bytes()).hexdigest(), self.checksum)

    def test_corrupt_cache_is_rejected_without_replacing_it(self):
        cached = self.run_pin()
        cached.chmod(0o600)
        cached.write_bytes(b'corrupt')
        inode = cached.stat().st_ino
        with self.assertRaisesRegex(ValueError, 'corrupted'):
            self.run_pin()
        self.assertEqual(cached.stat().st_ino, inode)
        self.assertEqual(cached.read_bytes(), b'corrupt')

    def test_source_mismatch_publishes_no_file(self):
        self.source.write_bytes(b'wrong runtime')
        with self.assertRaisesRegex(ValueError, 'source SHA256'):
            self.run_pin()
        self.assertFalse(list((self.root / 'runtime-rootfs').iterdir()))

    def test_source_and_cache_symlinks_are_rejected(self):
        original = self.source.with_suffix('.original')
        self.source.rename(original)
        self.source.symlink_to(original)
        with self.assertRaises(OSError):
            self.run_pin()
        self.source.unlink()
        original.rename(self.source)
        cached = self.run_pin()
        cached.unlink()
        cached.symlink_to(self.source)
        with self.assertRaises(OSError):
            self.run_pin(stage=False)

    def test_missing_staged_file_cannot_be_rebuilt_by_verify(self):
        with self.assertRaisesRegex(ValueError, 'directory is missing'):
            self.run_pin(stage=False)
        self.assertFalse((self.root / 'runtime-rootfs').exists())

    def test_fifo_is_rejected_without_blocking(self):
        self.source.unlink()
        os.mkfifo(self.source)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.run_pin()


if __name__ == '__main__':
    unittest.main()
