#!/usr/bin/env python3
"""Exercise the AKernel installer with a real candidate/release archive.

Usage: python3 builder/scripts/test-install-distill-fs.py /path/to/release.tar.gz
Requires curl, jq, binutils, and a Linux/amd64 host. No network is needed.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


archive = Path(sys.argv[1]).resolve()
installer = Path(__file__).with_name("install-distill-fs.sh")
with tarfile.open(archive) as bundle:
    files = {name: bundle.extractfile(name).read() for name in (
        "distill_fs", "manifest.json", "LICENSE", "NOTICE", "Cargo.lock"
    )}
manifest = json.loads(files["manifest.json"])
release = manifest["release_tag"]
digest = hashlib.sha256(archive.read_bytes()).hexdigest()

with tempfile.TemporaryDirectory(prefix="distill-fs-installer-") as work:
    work = Path(work)

    def check(name, asset=archive, checksum=digest, tag=release, arch="amd64", error=None):
        destination = work / name
        result = subprocess.run(
            ["sh", str(installer), tag, asset.as_uri(), checksum, str(destination)],
            env={**os.environ, "TARGETARCH": arch},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        binary = destination / "bin/distill_fs"
        if error is None:
            assert result.returncode == 0, result.stdout
            assert binary.read_bytes() == files["distill_fs"]
            for filename in ("manifest.json", "LICENSE", "NOTICE", "Cargo.lock"):
                assert (destination / "share/distill-fs" / filename).read_bytes() == files[filename]
        else:
            assert result.returncode != 0, name
            assert error in result.stdout, result.stdout
            assert not binary.exists(), "a rejected artifact must not be installed"
        print(f"PASS {name}")

    def altered_archive(name, binary, binary_hash):
        data = {**files, "distill_fs": binary}
        metadata = {**manifest, "binary_sha256": binary_hash}
        data["manifest.json"] = json.dumps(metadata).encode()
        path = work / f"{name}.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            for filename, content in data.items():
                entry = tarfile.TarInfo(filename)
                entry.size = len(content)
                tar.addfile(entry, io.BytesIO(content))
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    check("valid")
    check("missing-pin", checksum="", error="publish and pin")
    check("bad-pin", checksum="not-a-digest", error="invalid distill-fs SHA-256")
    check("corrupt-archive", checksum="0" * 64, error="FAILED")
    check("wrong-version", tag="v999.0.0", error="")
    check("unsupported-arch", arch="arm64", error="linux/amd64 only")
    bad, sha = altered_archive("bad-binary-hash", files["distill_fs"], "0" * 64)
    check("bad-binary-hash", asset=bad, checksum=sha, error="FAILED")
    # A correctly checksummed bundle must still reject a dynamic executable.
    dynamic = Path("/bin/true").read_bytes()
    headers = subprocess.check_output(["readelf", "-l", "/bin/true"], text=True)
    assert "INTERP" in headers, "this negative fixture needs a dynamic /bin/true"
    bad, sha = altered_archive("dynamic-binary", dynamic, hashlib.sha256(dynamic).hexdigest())
    check("dynamic-binary", asset=bad, checksum=sha, error="must be a static executable")
