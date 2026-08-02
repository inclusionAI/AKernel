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

"""AKernel SDK CLI — ak command for managing sandbox instances."""

import argparse
import json
import os
import ssl
import sys
import threading
from collections.abc import Sequence
from urllib import request
from urllib.error import HTTPError, URLError

from ._addresses import Endpoint, api_endpoint_from_env
from ._resource_api import (
    ResourceAPIError,
    query_resource_view,
)
from ._resource_api import (
    extract_labels as _extract_labels,
)
from ._resource_api import (
    extract_resources as _extract_resources,
)
from .pty import Pty, PtyError

DEFAULT_COMMAND = "/bin/bash"


def _get_auth_token() -> str:
    """Return the configured token or exit with a user-facing error."""

    token = os.environ.get("AKERNEL_TOKEN", "").strip()
    if not token:
        print("Error: AKERNEL_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    return token


def _get_endpoint(configure) -> Endpoint:
    try:
        return configure()
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


def _create_ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _make_get_request(url: str, token: str, ssl_context: ssl.SSLContext) -> dict:
    req = request.Request(url, method="GET", headers={"X-AUTH": token})
    try:
        with request.urlopen(req, context=ssl_context) as response:
            return {"status": response.status, "body": response.read().decode("utf-8")}
    except HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8")}
    except URLError as e:
        print(f"Error: failed to send request: {e}", file=sys.stderr)
        sys.exit(1)


def _make_json_request(
    url: str,
    token: str,
    ssl_context: ssl.SSLContext,
    payload: dict,
) -> dict:
    """Send an authenticated JSON request and return status and body."""

    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Auth": token,
        },
    )
    try:
        with request.urlopen(req, context=ssl_context) as response:
            return {
                "status": response.status,
                "body": response.read().decode("utf-8"),
            }
    except HTTPError as error:
        return {
            "status": error.code,
            "body": error.read().decode("utf-8"),
        }
    except URLError as error:
        raise RuntimeError(f"failed to send request: {error}") from error


def _fmt_cpu(millicores: float) -> str:
    """Format CPU value: use cores for >=1 core, millicores otherwise."""
    if millicores == 0:
        return "0"
    cores = millicores / 1000.0
    if cores >= 1.0:
        return f"{cores:.1f}"
    return f"{millicores:.0f}m"


def _fmt_mem(mb: float) -> str:
    """Format memory value in GB."""
    return f"{mb / 1024:.1f}G"


def _fmt_resource_count(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _fmt_xpu(capacity: dict[str, float], allocatable: dict[str, float]) -> str:
    values = []
    for name, total in sorted(capacity.items()):
        fields = name.split("/", 1)
        if len(fields) != 2 or fields[0].upper() not in {"GPU", "NPU", "TPU"}:
            continue
        available = allocatable.get(name, 0.0)
        values.append(
            f"{fields[0].lower()}/{fields[1]} "
            f"{_fmt_resource_count(available)}/{_fmt_resource_count(total)}"
        )
    return ", ".join(values) or "-"


def handle_resources(debug: bool = False):
    """Query cluster resource information and display in table format."""
    try:
        data = query_resource_view()
    except (ResourceAPIError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        if isinstance(error, ResourceAPIError) and error.body:
            print(f"Response: {error.body}", file=sys.stderr)
        sys.exit(1)

    if debug:
        print("=== Raw JSON Response ===")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print()

    # Response is QueryResourcesInfoResponse: {"requestID": "...", "resource": {...}}
    resource = data.get("resource", data) if isinstance(data, dict) else None
    if resource is None:
        print("No resource data in response.")
        return

    # The top-level resource is the domain scheduler (aggregate).
    # Actual nodes live in resource.fragment as a map of nodeId → ResourceUnit.
    fragment = resource.get("fragment", {}) if isinstance(resource, dict) else {}
    if fragment:
        units = list(fragment.values())
    else:
        units = [resource] if isinstance(resource, dict) else resource

    # ── Summary accumulators ──
    total_cpu_total = 0.0
    total_cpu_used = 0.0
    total_mem_total = 0.0
    total_mem_used = 0.0

    # ── Build per-node rows ──
    headers = [
        "ID",
        "STATUS",
        "CPU",
        "CPU USED",
        "CPU%",
        "MEM",
        "MEM USED",
        "MEM%",
        "XPU",
        "HOST IP",
    ]
    rows = []
    for u in units:
        nid = u.get("id", "-")
        status_val = u.get("status", 0)
        st = "OK" if status_val == 0 else str(status_val)

        capacity = _extract_resources(u.get("capacity", {}))
        allocatable = _extract_resources(u.get("allocatable", {}))
        labels = _extract_labels(u.get("nodeLabels", {}))

        cpu_total = capacity.get("CPU", 0)  # millicores
        cpu_al = allocatable.get("CPU", 0)  # millicores
        cpu_used = cpu_total - cpu_al  # millicores

        # Skip nodes with less than 1 core capacity
        if cpu_total < 1000:
            continue

        mem_total = capacity.get("Memory", 0)
        mem_al = allocatable.get("Memory", 0)
        mem_used = mem_total - mem_al
        host_ips = labels.get("HOST_IP", ["-"])
        host_ip = host_ips[0] if host_ips else "-"

        total_cpu_total += cpu_total / 1000.0
        total_cpu_used += cpu_used / 1000.0
        total_mem_total += mem_total
        total_mem_used += mem_used

        cpu_usage = (cpu_used / cpu_total) * 100 if cpu_total > 0 else 0
        mem_usage = (mem_used / mem_total) * 100 if mem_total > 0 else 0

        rows.append(
            [
                nid,
                st,
                _fmt_cpu(cpu_total),
                _fmt_cpu(cpu_used),
                f"{cpu_usage:.1f}%",
                _fmt_mem(mem_total),
                _fmt_mem(mem_used),
                f"{mem_usage:.1f}%",
                _fmt_xpu(capacity, allocatable),
                host_ip,
            ]
        )

    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def fmt_row(cells):
        return "  ".join(c.rjust(col_widths[i]) for i, c in enumerate(cells))

    # ── Summary ──
    cluster_cpu_usage = (
        (total_cpu_used / total_cpu_total) * 100 if total_cpu_total > 0 else 0
    )
    cluster_mem_usage = (
        (total_mem_used / total_mem_total) * 100 if total_mem_total > 0 else 0
    )

    print("=== Cluster Resource Summary ===")
    print(f"  Nodes:       {len(rows)}")
    print(f"  CPU  total:  {total_cpu_total:.1f} cores")
    print(f"  CPU  used:   {total_cpu_used:.1f} cores ({cluster_cpu_usage:.1f}%)")
    print(f"  MEM  total:  {_fmt_mem(total_mem_total)}")
    print(f"  MEM  used:   {_fmt_mem(total_mem_used)} ({cluster_mem_usage:.1f}%)")
    print()

    # ── Per-node table ──
    # Print header (left-aligned) and separator
    print("  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in col_widths))
    for row in rows:
        print(fmt_row(row))


def handle_list(quiet: bool = False):
    """List all running instances.

    When *quiet* is set, print only instance IDs (one per line) so the output
    pipes cleanly into ``xargs ak delete``.
    """
    endpoint = _get_endpoint(api_endpoint_from_env)
    token = _get_auth_token()
    ssl_context = _create_ssl_context()

    list_url = f"{endpoint.base_url()}/api/instances?tenant_id=default"
    result = _make_get_request(list_url, token, ssl_context)

    if result["status"] != 200:
        print(f"Error: server returned status {result['status']}", file=sys.stderr)
        print(f"Response: {result['body']}", file=sys.stderr)
        sys.exit(1)

    try:
        instances = json.loads(result["body"])
    except json.JSONDecodeError as e:
        print(f"Error: failed to parse response: {e}", file=sys.stderr)
        sys.exit(1)

    running = [inst for inst in instances if inst.get("status") == "running"]

    if quiet:
        for inst in running:
            inst_id = inst.get("id", "")
            if inst_id:
                print(inst_id)
        return

    if not running:
        print("No running instances found.")
        return

    # Print header
    id_width = max(len(inst.get("id", "")) for inst in running)
    id_width = max(id_width, 2)

    print(f"{'ID':<{id_width}}  STATUS")
    print(f"{'-' * id_width}  ------")
    for inst in running:
        inst_id = inst.get("id", "unknown")
        status = inst.get("status", "unknown")
        print(f"{inst_id:<{id_width}}  {status}")


def handle_delete(instance_ids: list[str]) -> None:
    """Terminate sandbox instances through the frontend actor API."""

    endpoint = _get_endpoint(api_endpoint_from_env)
    token = _get_auth_token()
    ssl_context = _create_ssl_context()
    failed = []
    for instance_id in instance_ids:
        try:
            result = _make_json_request(
                f"{endpoint.base_url()}/frontend/v1/instance/kill",
                token,
                ssl_context,
                {"instanceID": instance_id, "signal": 1},
            )
            if result["status"] != 200:
                raise RuntimeError(
                    f"server returned status {result['status']}: {result['body']}"
                )
            try:
                body = json.loads(result["body"])
            except json.JSONDecodeError as error:
                raise RuntimeError(f"invalid server response: {error}") from error
            if not isinstance(body, dict):
                raise RuntimeError("invalid server response: expected a JSON object")
            code = int(body.get("code", -1))
            if code != 0:
                message = body.get("message") or "unknown error"
                raise RuntimeError(f"server returned code {code}: {message}")
            print(f"deleted: {instance_id}")
        except Exception as error:
            print(f"failed to delete {instance_id}: {error}", file=sys.stderr)
            failed.append(instance_id)

    if failed:
        sys.exit(1)


class _RawTerminal:
    """Context manager for raw terminal mode."""

    def __init__(self, fd):
        import termios

        self.fd = fd
        self.old = termios.tcgetattr(fd)

    def __enter__(self):
        import tty

        tty.setraw(self.fd)
        return self

    def __exit__(self, *exc):
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def _terminal_size() -> tuple[int, int]:
    import shutil

    try:
        terminal_size = shutil.get_terminal_size()
        return terminal_size.lines, terminal_size.columns
    except Exception:
        return 24, 80


def _write_terminal_data(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def handle_exec(instance_id: str, command: str | Sequence[str]) -> int:
    """Attach the local terminal to an interactive sandbox process."""

    import contextlib
    import select
    import signal

    rows, cols = _terminal_size()
    resize_pending = threading.Event()
    previous_sigwinch = None

    print(f"Connecting to {instance_id}...", file=sys.stderr)
    print("Press Ctrl+] to disconnect", file=sys.stderr)

    try:
        session = Pty(instance_id).create(
            command,
            rows=rows,
            cols=cols,
            on_data=_write_terminal_data,
        )

        def request_resize(_signum, _frame):
            resize_pending.set()

        previous_sigwinch = signal.signal(signal.SIGWINCH, request_resize)
        stdin_fd = sys.stdin.fileno()
        terminal_context = (
            _RawTerminal(stdin_fd) if sys.stdin.isatty() else contextlib.nullcontext()
        )

        with session, terminal_context:
            while not session.done:
                if resize_pending.is_set():
                    resize_pending.clear()
                    new_rows, new_cols = _terminal_size()
                    session.resize(rows=new_rows, cols=new_cols)

                ready, _, _ = select.select([stdin_fd], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(stdin_fd, 4096)
                if not data:
                    session.close_stdin()
                    return session.wait()
                if b"\x1d" in data:
                    print(
                        "\n[Escape sequence detected, disconnecting...]",
                        file=sys.stderr,
                    )
                    session.close()
                    return 0
                session.send_stdin(data)
            return session.wait()
    except KeyboardInterrupt:
        print("\nDisconnected", file=sys.stderr)
        return 130
    except (PtyError, TimeoutError, OSError) as error:
        print(f"\nConnection error: {error}", file=sys.stderr)
        return 1
    finally:
        if previous_sigwinch is not None:
            signal.signal(signal.SIGWINCH, previous_sigwinch)


def main():
    parser = argparse.ArgumentParser(prog="ak", description="AKernel SDK CLI")
    sub = parser.add_subparsers(dest="command")

    resources_parser = sub.add_parser(
        "resources", help="Query cluster resource information"
    )
    resources_parser.add_argument(
        "--debug",
        action="store_true",
        help="Print raw JSON response for debugging",
    )

    list_parser = sub.add_parser("list", help="List all running instances")
    list_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Print only instance IDs (suitable for `ak list -q | xargs ak delete`)",
    )

    exec_parser = sub.add_parser("exec", help="Execute command in a sandbox instance")
    exec_parser.add_argument("instance_id", help="Instance ID")
    exec_parser.add_argument(
        "cmdline",
        nargs="*",
        default=None,
        help="Command to execute (default: /bin/bash)",
    )

    delete_parser = sub.add_parser(
        "delete", help="Terminate one or more sandbox instances"
    )
    delete_parser.add_argument(
        "instance_ids",
        nargs="+",
        help="Instance IDs to delete (one or more)",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "resources":
        handle_resources(debug=args.debug)
    elif args.command == "list":
        handle_list(quiet=args.quiet)
    elif args.command == "exec":
        cmd = args.cmdline if args.cmdline else [DEFAULT_COMMAND]
        sys.exit(handle_exec(args.instance_id, cmd))
    elif args.command == "delete":
        handle_delete(args.instance_ids)


if __name__ == "__main__":
    main()
