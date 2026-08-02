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

"""Run a CUDA sample in an experimental gVisor GPU sandbox."""

import os

from akernel_sdk import Sandbox

CUDA_SAMPLE_IMAGE = (
    "nvcr.io/nvidia/k8s/cuda-sample@"
    "sha256:95ce52d6e3b11783606152f4da94af9cf84e7ca4dd63eb03c95edcc5b7bba8d9"
)


def main() -> None:
    model = os.environ.get("AKERNEL_GPU_MODEL", "a10").strip().lower()
    with Sandbox(
        image=CUDA_SAMPLE_IMAGE,
        runtime="runsc",
        xpu=f"gpu:{model}:1",
        storage_mb=512,
        cpu=1000,
        memory=2048,
        schedule_timeout=120,
    ) as sandbox:
        gpu = sandbox.commands.run(
            "nvidia-smi --query-gpu=index,name,uuid,driver_version "
            "--format=csv,noheader"
        )
        assert gpu.exit_code == 0, gpu.stderr
        assert len(gpu.stdout.strip().splitlines()) == 1, gpu.stdout
        print(gpu.stdout.strip())

        vector_add = sandbox.commands.run("/cuda-samples/vectorAdd")
        assert vector_add.exit_code == 0, vector_add.stderr
        assert "Test PASSED" in vector_add.stdout, vector_add.stdout
        print("CUDA vectorAdd: PASSED")


if __name__ == "__main__":
    main()
