"""Dependency-free helper executed inside the selected service's PID namespace."""

import json
import os
import signal
import sys
import time
from pathlib import Path


def stat(pid):
    try:
        value = Path("/proc/" + str(pid) + "/stat").read_text()
        fields = value[value.rindex(")") + 2 :].split()
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "state": fields[0],
            "comm": value[value.index("(") + 1 : value.rindex(")")],
            "start_time": fields[19],
        }
    except (OSError, ValueError):
        return None


def write(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def start(root, config):
    # Exclusive reservation prevents replay of an ambiguous launch.
    root.mkdir(mode=0o700, exist_ok=False)
    monitor = os.fork()
    if monitor == 0:
        os.setsid()
        null = os.open("/dev/null", os.O_RDWR)
        for number in (0, 1, 2):
            os.dup2(null, number)
        os.close(null)
        pid = os.fork()
        if pid == 0:
            try:
                for number, name in ((1, "stdout"), (2, "stderr")):
                    descriptor = os.open(
                        str(root / name), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                    )
                    os.dup2(descriptor, number)
                    os.close(descriptor)
                os.execvpe(
                    config["shell"],
                    [config["shell"], "-c", config["command"]],
                    os.environ,
                )
            except BaseException:
                os._exit(127)
        record = stat(pid)
        if record is not None:
            record["monitor_pid"] = os.getpid()
            write(root / "identity.json", record)
        _, status = os.waitpid(pid, 0)
        code = (
            os.WEXITSTATUS(status)
            if os.WIFEXITED(status)
            else 128 + os.WTERMSIG(status)
        )
        write(root / "exit.json", {"exit_code": code})
        os._exit(0)
    deadline = time.monotonic() + 10
    while not (root / "identity.json").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("process launch outcome unknown; do not resubmit")
        time.sleep(0.02)
    return json.loads((root / "identity.json").read_text())


def inspect(root):
    record = json.loads((root / "identity.json").read_text())
    current = stat(record["pid"])
    if current is None or current["state"] == "Z":
        return []
    if current["start_time"] != record["start_time"]:
        raise RuntimeError("process identity mismatch; refusing reused PID")
    records = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            row = stat(int(name))
            if row is not None:
                records[row["pid"]] = row
    selected = {record["pid"]}
    while True:
        children = {
            pid for pid, row in records.items() if row["ppid"] in selected
        } - selected
        if not children:
            break
        selected.update(children)
    return [records[pid] for pid in sorted(selected) if pid in records]


def operate(root, action):
    rows = inspect(root)
    if action == "inspect":
        return rows
    signals = {
        "stop": signal.SIGSTOP,
        "continue": signal.SIGCONT,
        "terminate": signal.SIGTERM,
        "kill": signal.SIGKILL,
    }
    if action not in signals:
        raise ValueError("unsupported process action")
    for _ in range(3 if action == "stop" else 1):
        parents = {row["pid"]: row["ppid"] for row in rows}

        def depth(row, parents=parents):
            pid, visited = row["pid"], set()
            while pid in parents and pid not in visited:
                visited.add(pid)
                pid = parents[pid]
            return len(visited)

        # Freeze waiters before children. Thaw children before waiters so Bash
        # cannot consume a pending child-stop notification as exit status 147.
        # PID numeric ordering does not encode ancestry after PID reuse/wrap.
        for row in sorted(rows, key=depth, reverse=action == "continue"):
            current = stat(row["pid"])
            if current is not None and current["start_time"] == row["start_time"]:
                try:
                    os.kill(row["pid"], signals[action])
                except ProcessLookupError:
                    pass
        if action == "stop":
            time.sleep(0.03)
            rows = inspect(root)
    return rows


def main():
    operation, identity, argument = sys.argv[1:4]
    if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("invalid process identity")
    root = Path("/tmp/akernel-service-process-" + identity)
    if operation == "start":
        result = start(root, json.loads(argument))
    elif operation == "signal":
        request = json.loads(argument)
        expected = json.loads((root / "identity.json").read_text())
        if (
            request.get("pid") != expected["pid"]
            or request.get("start_time") != expected["start_time"]
        ):
            raise RuntimeError("process identity record changed")
        result = operate(root, request["action"])
    elif operation == "status":
        result = {
            "processes": inspect(root),
            "identity": json.loads((root / "identity.json").read_text()),
            "exit_code": None,
        }
        if (root / "exit.json").exists():
            result.update(json.loads((root / "exit.json").read_text()))
    else:
        raise ValueError("unsupported process operation")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
