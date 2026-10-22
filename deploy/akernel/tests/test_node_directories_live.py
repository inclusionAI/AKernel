#!/usr/bin/env python3
"""Opt-in SDK acceptance against an operator-prepared, disposable directory.

Use an installed SDK and a dedicated node-visible source. This test owns only
its uniquely named files; the operator removes them after physical cleanup.
It never prepares sources, changes Kubernetes resources or deletes host data.
"""

import json
import os
import shlex
import time
import unittest
import uuid


ENABLED = os.environ.get("AKERNEL_TEST_NODE_DIRECTORIES") == "1"
SOURCE = os.environ.get("AKERNEL_TEST_HOSTPATH_SOURCE", "")
NODE = os.environ.get("AKERNEL_TEST_NODE_ID", "")
IMAGE = os.environ.get("AKERNEL_TEST_IMAGE", "python:3.12-slim")
CASE = "directory-" + uuid.uuid4().hex
TARGET = "/app/logs"
EVENTS = []


def event(**value):
    EVENTS.append(value)
    print(json.dumps(value), flush=True)


@unittest.skipUnless(ENABLED and SOURCE and NODE, "requires explicit opt-in, prepared source and node")
class NodeDirectoriesLiveTest(unittest.TestCase):
    def sandbox(self, read_only=False, failover=False):
        from akernel_sdk import Mount, Sandbox

        sandbox = Sandbox(image=IMAGE, runtime="runsc", node_id=NODE,
                          mounts=[Mount(target=TARGET, host_path=SOURCE, read_only=read_only)],
                          cpu=1000, memory=1024, idle_timeout=300, failover=failover)
        event(action="created", id=sandbox.id, node=NODE, read_only=read_only, failover=failover)
        return sandbox

    def command(self, sandbox, code, timeout=60):
        result = sandbox.commands.run("python3 -c " + shlex.quote(code), timeout=timeout)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return result.stdout

    def wait_file(self, sandbox, path):
        code = f"import time;from pathlib import Path;p=Path({path!r});end=time.monotonic()+60\nwhile not p.exists() and time.monotonic()<end: time.sleep(.1)\nassert p.exists(),str(p)"
        self.command(sandbox, code, timeout=70)

    def test_ownership_read_only_and_retention(self):
        name = CASE + "-uid.log"
        with self.sandbox() as sandbox:
            code = f"""import json,os,stat
from pathlib import Path
os.setgroups([])
os.setgid(1000)
os.setuid(1000)
os.umask(0o027)
p=Path({TARGET!r})/{name!r}
p.write_text('application-data\\n')
s=p.stat()
print(json.dumps(dict(uid=s.st_uid,gid=s.st_gid,mode=stat.S_IMODE(s.st_mode))))
"""
            metadata = json.loads(self.command(sandbox, code))
            self.assertEqual(metadata, {"uid": 1000, "gid": 1000, "mode": 0o640})
        with self.sandbox(read_only=True) as sandbox:
            self.assertEqual(sandbox.files.read(TARGET + "/" + name), "application-data\n")
            code = f"""import errno,os
root={TARGET!r}
for name,flags in [({name!r},os.O_WRONLY|os.O_TRUNC),({CASE + '-forbidden'!r},os.O_WRONLY|os.O_CREAT)]:
 try: fd=os.open(root+'/'+name,flags,0o600)
 except OSError as e: assert e.errno==errno.EROFS,e
 else: os.close(fd);raise AssertionError('read-only bind accepted write')
"""
            self.command(sandbox, code)
        event(action="ownership-ro-retention-passed", node=NODE, file=name, metadata=metadata)

    @unittest.skipUnless(os.environ.get("AKERNEL_TEST_HOSTPATH_CHECKPOINT") == "1", "requires runsc checkpoint opt-in")
    def test_external_state_and_continued_append_after_reload(self):
        prefix = TARGET + "/" + CASE
        with self.sandbox(failover=True) as sandbox:
            logger = f"""import os,time
from pathlib import Path
os.setgroups([]);os.setgid(1000);os.setuid(1000)
prefix={prefix!r}
with open(prefix+'-starts','a') as f: f.write('started\\n')
fd=os.open(prefix+'-append.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o640)
os.write(fd,b'before-checkpoint\\n')
memory_value=123
Path(prefix+'-ready').write_text('ready')
while not Path(prefix+'-go').exists(): time.sleep(.1)
os.write(fd,('resumed-memory=%s\\n'%memory_value).encode())
Path(prefix+'-resumed').write_text('resumed')
while True: time.sleep(1)
"""
            sandbox.commands.run("python3 -u -c " + shlex.quote(logger), background=True)
            self.wait_file(sandbox, prefix + "-ready")
            checkpoint = """import socket
c=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
c.settimeout(240)
c.connect('/run/akernel/execd.sock')
c.sendall(b'POST /checkpoint HTTP/1.1\\r\\nHost: localhost\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n')
r=b''.join(iter(lambda:c.recv(65536),b''))
assert b' 200 ' in r.split(b'\\r\\n',1)[0],r
assert b'"status":"completed"' in r,r
print('checkpoint-completed')
"""
            self.command(sandbox, checkpoint, timeout=250)
            self.command(sandbox, f"with open({prefix + '-append.log'!r},'a') as f: f.write('after-checkpoint\\n')")
            self.assertTrue(sandbox.reload())
            sandbox.files.write(prefix + "-go", "go")
            self.wait_file(sandbox, prefix + "-resumed")
            self.assertEqual(sandbox.files.read(prefix + "-append.log"),
                             "before-checkpoint\nafter-checkpoint\nresumed-memory=123\n")
            self.assertEqual(sandbox.files.read(prefix + "-starts"), "started\n")
        event(action="checkpoint-external-append-passed", node=NODE, prefix=CASE)


if __name__ == "__main__":
    unittest.main()
