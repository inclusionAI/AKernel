# Copyright (c) 2026 Ant Group Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Measure verified upload/download round trips through the public SDK."""

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from akernel_sdk import Sandbox
from benchmarks.harness.errors import safe_error
from benchmarks.harness.stats import LatencyHistogram


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generate_file(path: Path, size: int) -> None:
    """Write reproducible binary data without changing global RNG state."""
    if size < 1:
        raise ValueError("size must be positive")
    chunk = bytes((index * 17 + 31) % 256 for index in range(64 * 1024))
    with path.open("wb") as target:
        full, remainder = divmod(size, len(chunk))
        for _ in range(full):
            target.write(chunk)
        target.write(chunk[:remainder])


def run_file_case(
    files,
    source: Path,
    remote: str,
    *,
    method: str,
    destination: Path,
) -> dict:
    """Upload, read back, download, hash-check, and remove one remote file."""
    if method not in {"copy_from_local", "write"}:
        raise ValueError(f"unsupported upload method: {method}")
    local_download = destination / "download.bin"
    expected_digest = _digest(source)
    started = time.perf_counter()
    try:
        if method == "copy_from_local":
            files.copy_from_local(str(source), remote)
        else:
            files.write(remote, source.read_bytes())
        uploaded = time.perf_counter()

        read_back = files.read(remote, format="bytes")
        if hashlib.sha256(read_back).hexdigest() != expected_digest:
            raise AssertionError("remote read hash mismatch")
        read_done = time.perf_counter()

        files.copy_to_local(remote, str(local_download))
        downloaded = time.perf_counter()
        if _digest(local_download) != expected_digest:
            raise AssertionError("download hash mismatch")

        return {
            "method": method,
            "size_bytes": source.stat().st_size,
            "sha256": expected_digest,
            "upload_seconds": uploaded - started,
            "read_seconds": read_done - uploaded,
            "download_seconds": downloaded - read_done,
            "full_seconds": time.perf_counter() - started,
        }
    finally:
        try:
            files.remove(remote)
        finally:
            local_download.unlink(missing_ok=True)


def run_benchmark(
    *,
    sizes: tuple[int, ...],
    iterations: int,
    runtime: str,
    image: str | None,
    cpu: int,
    memory: int,
    run_id: str,
) -> dict:
    if not sizes or any(size < 1 for size in sizes) or iterations < 1:
        raise ValueError("sizes and iterations must be positive")
    cases = []
    errors = []
    histograms: dict[str, LatencyHistogram] = {}
    with tempfile.TemporaryDirectory(prefix="akernel-bench-cp-") as temp_dir:
        temporary = Path(temp_dir)
        with Sandbox(runtime=runtime, image=image, cpu=cpu, memory=memory) as sandbox:
            for size in sizes:
                source = temporary / f"payload-{size}.bin"
                generate_file(source, size)
                for method in ("copy_from_local", "write"):
                    for iteration in range(iterations):
                        remote = (
                            f"/tmp/akernel-bench-{run_id}-{size}-{method}-{iteration}"
                        )
                        try:
                            case = run_file_case(
                                sandbox.files,
                                source,
                                remote,
                                method=method,
                                destination=temporary,
                            )
                        except Exception as error:
                            errors.append(
                                {
                                    "size_bytes": size,
                                    "method": method,
                                    "iteration": iteration,
                                    "error": safe_error(error),
                                }
                            )
                            continue
                        cases.append(case)
                        key = f"{size}:{method}"
                        histograms.setdefault(key, LatencyHistogram()).observe(
                            case["full_seconds"]
                        )
    return {
        "schema_version": 1,
        "run_id": run_id,
        "status": "passed" if not errors else "failed",
        "config": {
            "sizes_bytes": list(sizes),
            "iterations": iterations,
            "runtime": runtime,
            "image": image,
        },
        "completed_cases": len(cases),
        "failed_cases": len(errors),
        "cases": cases,
        "latency_ms": {key: value.summary() for key, value in histograms.items()},
        "errors": errors[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verified AKernel SDK file benchmark")
    parser.add_argument("--sizes", default="1024,1048576,33554432")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--runtime", default="runsc")
    parser.add_argument("--image", default="")
    parser.add_argument("--cpu", type=int, default=1000)
    parser.add_argument("--memory", type=int, default=2048)
    parser.add_argument("--run-id", default=uuid4().hex)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        sizes = tuple(int(item) for item in args.sizes.split(","))
        result = run_benchmark(
            sizes=sizes,
            iterations=args.iterations,
            runtime=args.runtime,
            image=args.image or None,
            cpu=args.cpu,
            memory=args.memory,
            run_id=args.run_id,
        )
    except Exception as error:
        result = {
            "schema_version": 1,
            "run_id": args.run_id,
            "status": "driver_error",
            "error": safe_error(error),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"file benchmark status={result['status']} result={args.output}")
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
