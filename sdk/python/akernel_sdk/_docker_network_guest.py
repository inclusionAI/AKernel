"""Dependency-free networking helper, executed in the outer sandbox only.

The runtime owns forwarding/NAT; Docker owns bridges, IPAM, DNS and its
userland port proxies. No rules or listeners are installed on the real host.
"""

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

STATE = Path("/opt/akernel-docker-runtime/network.json")


def configure(*, reconnect=False):
    route = subprocess.check_output(
        ["ip", "-4", "route", "get", "1.1.1.1"], text=True
    ).split()
    device = route[route.index("dev") + 1]
    source = str(ipaddress.IPv4Address(route[route.index("src") + 1]))
    executable = shutil.which("iptables-legacy")
    restore = shutil.which("iptables-legacy-restore")
    if executable is None or restore is None:
        raise RuntimeError(
            "Managed bridge networking requires iptables-legacy "
            "and iptables-legacy-restore"
        )
    record = {"uplink": device, "source": source, "iptables": executable}
    if reconnect:
        previous = json.loads(STATE.read_text())
        if previous == record:
            return record
    else:
        # Docker's embedded DNS still programs per-container DNAT rules with
        # --iptables=false. Put legacy tools in only the daemon's PATH.
        directory = STATE.parent / "network-bin"
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("iptables", "ip6tables"):
            legacy = shutil.which(name + "-legacy")
            if legacy is not None:
                (directory / name).symlink_to(legacy)
        Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
        subprocess.run([executable, "-w", "-P", "FORWARD", "DROP"], check=True)
        for table, hook, chain in (
            ("filter", "FORWARD", "AKERNEL_DIND_FWD"),
            ("nat", "POSTROUTING", "AKERNEL_DIND_NAT"),
        ):
            prefix = [executable, "-w", "-t", table]
            subprocess.run([*prefix, "-N", chain], check=True)
            subprocess.run([*prefix, "-A", hook, "-j", chain], check=True)
    # A restored sandbox has a new outer TAP name/address. Replace only owned
    # chains, atomically per table, keeping Docker's bridges, DNS, proxies and
    # existing inner connections alive. Never restart the daemon or Compose.
    rules = (
        "*filter\n:AKERNEL_DIND_FWD - [0:0]\n"
        f"-A AKERNEL_DIND_FWD -i {device} -j ACCEPT\n"
        f"-A AKERNEL_DIND_FWD -o {device} -j ACCEPT\nCOMMIT\n"
        "*nat\n:AKERNEL_DIND_NAT - [0:0]\n"
        f"-A AKERNEL_DIND_NAT -o {device} -p tcp -j SNAT --to-source {source}\n"
        f"-A AKERNEL_DIND_NAT -o {device} -p udp -j SNAT --to-source {source}\n"
        "COMMIT\n"
    )
    subprocess.run([restore, "--wait", "--noflush"], input=rules, text=True, check=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(STATE)
    return record


def forward(peer, target):
    """Relay bytes and half-closes without interpreting HTTP, auth or SSE."""
    try:
        upstream = socket.create_connection(target, timeout=10)
    except OSError:
        peer.close()
        return

    def pump(source, destination):
        try:
            while chunk := source.recv(65536):
                destination.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    with peer, upstream:
        peer.settimeout(None)
        upstream.settimeout(None)
        other = threading.Thread(target=pump, args=(upstream, peer), daemon=True)
        other.start()
        pump(peer, upstream)
        other.join()


def serve(listener, target, allowed):
    while True:
        try:
            peer, address = listener.accept()
        except OSError:
            return
        if address[0] not in allowed:
            peer.close()
            continue
        threading.Thread(target=forward, args=(peer, target), daemon=True).start()


def expose(root, config):
    # The caller reserves this identity before dispatch. Never replay an
    # ambiguous start, or expose a wildcard/unrestricted outer listener.
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    address = str(ipaddress.IPv4Address(config["address"]))
    allowed = {str(ipaddress.IPv4Address(v)) for v in config["allowed"]}
    if not allowed or address in {"0.0.0.0", "127.0.0.1"}:
        raise ValueError("A service gateway and explicit peer addresses are required")
    port = config["target_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Invalid loopback tunnel port")
    listener = socket.socket()
    listener.bind((address, 0))
    listener.listen(64)
    record = {"url": f"http://{address}:{listener.getsockname()[1]}"}
    pid = os.fork()
    if pid == 0:
        os.setsid()
        # The listener must outlive the SDK command's stdin/stdout transport.
        if os.fork() != 0:
            os._exit(0)
        descriptor = os.open("/dev/null", os.O_RDWR)
        for number in (0, 1, 2):
            os.dup2(descriptor, number)
        if descriptor > 2:
            os.close(descriptor)
        serve(listener, ("127.0.0.1", port), allowed)
        os._exit(0)
    listener.close()
    _, status = os.waitpid(pid, 0)
    if status != 0:
        raise RuntimeError("Tunnel relay could not detach; launch outcome unknown")
    (root / "endpoint.json").write_text(json.dumps(record))
    return record


if __name__ == "__main__":
    if sys.argv[1] == "bootstrap":
        result = configure()
    elif sys.argv[1] == "reconnect":
        result = configure(reconnect=True)
    elif sys.argv[1] == "expose":
        result = expose(Path(sys.argv[2]), json.loads(sys.argv[3]))
    else:
        raise ValueError("Unknown Docker networking operation")
    print(json.dumps(result))
