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

import asyncio
import os
import tempfile
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, TypeVar, overload

import yr

from .._addresses import exec_endpoint_from_env
from ..types import EntryInfo

_T = TypeVar("_T")


def _run_coroutine(coroutine: Coroutine[Any, Any, _T]) -> _T:
    """Run a coroutine from a synchronous API in any caller context."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    # asyncio.run() cannot execute in a thread that already owns a running
    # event loop. Keep Filesystem's public API synchronous and isolate the
    # underlying async WebSocket helper in a short-lived worker thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


class Filesystem:
    """Client-side wrapper for filesystem operations on the remote sandbox."""

    def __init__(
        self,
        instance: Any,
        instance_id: str | None = None,
    ) -> None:
        self._instance = instance
        self._instance_id = instance_id

    @overload
    def read(self, path: str, format: Literal["text"] = "text") -> str: ...

    @overload
    def read(self, path: str, format: Literal["bytes"]) -> bytes: ...

    def read(self, path: str, format: str = "text") -> str | bytes:
        """Read a file as text or bytes.

        Args:
            path: Absolute or relative path inside the sandbox.
            format: Return decoded text or raw bytes.

        Raises:
            ValueError: ``format`` is not ``"text"`` or ``"bytes"``.
            RuntimeError: The sandbox cannot read the file.
        """

        if format not in ("text", "bytes"):
            raise ValueError("format must be 'text' or 'bytes'")
        binary = format == "bytes"
        result = yr.get(self._instance.fs_read.invoke(path=path, binary=binary))
        if result.get("error"):
            raise RuntimeError(f"Failed to read {path}: {result['error']}")
        data = result["data"]
        if binary:
            return bytes.fromhex(data)
        return data

    # Binary data beyond this threshold is routed through copy_from_local
    # (tar+WebSocket) instead of RPC hex encoding.  Benchmark shows the
    # crossover at ~16 MB where hex-encoded RPC starts to degrade while
    # copy_from_local's fixed connection overhead is amortized.
    _WRITE_SIZE_THRESHOLD = 16 * 1024 * 1024

    def write(self, path: str, data: str | bytes) -> EntryInfo:
        """Write text or bytes and return metadata for the remote file."""

        if isinstance(data, bytes):
            if len(data) >= self._WRITE_SIZE_THRESHOLD:
                return self._write_via_copy(path, data)
            binary = True
            send_data = data.hex()
        else:
            binary = False
            send_data = data
        result = yr.get(
            self._instance.fs_write.invoke(path=path, data=send_data, binary=binary)
        )
        if result.get("error"):
            raise RuntimeError(f"Failed to write {path}: {result['error']}")
        return EntryInfo(
            name=result["name"],
            path=result["path"],
            type=result["type"],
            size=result["size"],
            permissions="",
            modified_time=0.0,
        )

    def _write_via_copy(self, remote_path: str, data: bytes) -> EntryInfo:
        """Write large binary data via copy_from_local to avoid hex bloat."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        try:
            tmp.write(data)
            tmp.close()
            self.copy_from_local(tmp.name, remote_path)
        finally:
            os.unlink(tmp.name)
        return EntryInfo(
            name=os.path.basename(remote_path),
            path=remote_path,
            type="file",
            size=len(data),
            permissions="",
            modified_time=0.0,
        )

    def list(self, path: str, depth: int = 1) -> list[EntryInfo]:
        """List entries below a sandbox path up to ``depth`` levels."""

        result = yr.get(self._instance.fs_list.invoke(path=path, depth=depth))
        if result.get("error"):
            raise RuntimeError(f"Failed to list {path}: {result['error']}")
        return [
            EntryInfo(
                name=e["name"],
                path=e["path"],
                type=e["type"],
                size=e["size"],
                permissions=e["permissions"],
                modified_time=e["modified_time"],
            )
            for e in result["entries"]
        ]

    def exists(self, path: str) -> bool:
        """Return whether a sandbox path exists."""

        result = yr.get(self._instance.fs_exists.invoke(path=path))
        return result["exists"]

    def _get_connection(self) -> tuple[str, str, str, str, bool]:
        # File copy is implemented by the frontend exec WebSocket
        # (/terminal/ws), so it follows AKERNEL_SERVER_ADDRESS and uses the
        # same TLS setting as the frontend API.
        endpoint = exec_endpoint_from_env()
        instance_id = self._instance_id
        if not instance_id:
            raise RuntimeError("Instance ID is not set.")
        from yr.config_manager import ConfigManager

        token = ConfigManager().auth_token
        return endpoint.host, str(endpoint.port), instance_id, token, endpoint.use_tls

    def _cp(self, src: str, dst: str, upload: bool) -> None:
        from yr.cli.exec import (
            CopyRequest,
            ExecConnection,
            choose_cp_mode,
            copy_from_remote,
            copy_from_remote_streaming,
            copy_to_remote,
            copy_to_remote_streaming,
        )

        host, port, instance_id, token, use_ssl = self._get_connection()
        local_path = src if upload else dst
        remote_path = dst if upload else src

        if upload and not os.path.exists(local_path):
            raise FileNotFoundError(f"Local source path not found: {local_path}")

        streaming = choose_cp_mode(local_path, remote_path, upload=upload)
        conn = ExecConnection(
            host=host,
            port=port,
            use_ssl=use_ssl,
            verify_server=False,
            token=token,
        )
        request = CopyRequest(
            instance=instance_id,
            local_path=local_path,
            remote_path=remote_path,
        )

        if upload:
            fn = copy_to_remote_streaming if streaming else copy_to_remote
        else:
            fn = copy_from_remote_streaming if streaming else copy_from_remote

        _run_coroutine(fn(conn, request))

    def copy_from_local(self, local_path: str, remote_path: str) -> None:
        """Copy a local file or directory **into** the sandbox.

        Args:
            local_path: Absolute or relative path on the **local** machine.
            remote_path: Absolute path inside the **sandbox**.

        Raises:
            FileNotFoundError: *local_path* does not exist.
            RuntimeError: Server address is not configured.
        """
        self._cp(local_path, remote_path, upload=True)

    def copy_to_local(self, remote_path: str, local_path: str) -> None:
        """Copy a file or directory **from** the sandbox to the local machine.

        Args:
            remote_path: Absolute path inside the **sandbox**.
            local_path: Absolute or relative path on the **local** machine.

        Raises:
            RuntimeError: Server address is not configured.
        """
        self._cp(remote_path, local_path, upload=False)

    def remove(self, path: str) -> None:
        """Remove a file or directory from the sandbox."""

        result = yr.get(self._instance.fs_remove.invoke(path=path))
        if result.get("error"):
            raise RuntimeError(f"Failed to remove {path}: {result['error']}")

    def rename(self, old_path: str, new_path: str) -> EntryInfo:
        """Rename a sandbox path and return its updated metadata."""

        result = yr.get(
            self._instance.fs_rename.invoke(old_path=old_path, new_path=new_path)
        )
        if result.get("error"):
            raise RuntimeError(
                f"Failed to rename {old_path} -> {new_path}: {result['error']}"
            )
        return EntryInfo(
            name=result["name"],
            path=result["path"],
            type=result["type"],
            size=result["size"],
            permissions=result["permissions"],
            modified_time=result["modified_time"],
        )

    def make_dir(self, path: str) -> bool:
        """Create a directory and return whether it was newly created."""

        result = yr.get(self._instance.fs_make_dir.invoke(path=path))
        if result.get("error"):
            raise RuntimeError(f"Failed to make dir {path}: {result['error']}")
        return result["created"]

    def get_info(self, path: str) -> EntryInfo:
        """Return metadata for a sandbox path."""

        result = yr.get(self._instance.fs_get_info.invoke(path=path))
        if result.get("error"):
            raise RuntimeError(f"Failed to get info for {path}: {result['error']}")
        return EntryInfo(
            name=result["name"],
            path=result["path"],
            type=result["type"],
            size=result["size"],
            permissions=result["permissions"],
            modified_time=result["modified_time"],
        )
