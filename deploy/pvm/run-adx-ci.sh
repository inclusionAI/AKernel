#!/usr/bin/env bash
# Exact PVM candidate and Agent DX standalone qualification in the owned L1.
set -Eeuo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
out="${PVM_CI_OUTPUT:-${root}/.local/pvm-ci}"
mkdir -p "${out}"
python3 "${root}/src/sandboxd/test/e2e/pvm-l1.py" > "${out}/l1-before.json"
[[ "${EUID}" == 0 && "${DOCKER_HOST:-}" == unix:///run/pvm-ci-docker.sock ]]
for process in firecracker sandboxd virtiofsd; do
  if pgrep -x "${process}"; then
    echo "PVM CI refuses an existing ${process} process" >&2
    exit 1
  fi
done
if [[ ! -d /sys/module/kvm_pvm ]]; then
  [[ ! -d /sys/module/kvm ]]
  modprobe irqbypass
  insmod /opt/akernel-pvm-ci/modules/kvm.ko
  insmod /opt/akernel-pvm-ci/modules/kvm-pvm.ko
fi
python3 "${root}/src/sandboxd/test/e2e/pvm-l1.py" --probe > "${out}/abi.json"
for module in erofs fuse loop tun br_netfilter ip_tables ip6_tables \
  iptable_filter ip6table_filter iptable_nat ip6table_nat xt_conntrack xt_connmark; do
  modprobe "${module}"
done
cd "${root}"
distribution=$(python3 -c 'import json;print(json.load(open("src/sandboxd/third_party/firecracker-pvm.json")).get("distribution","actions"))')
if [[ "${distribution}" == actions ]]; then
  export PVM_CANDIDATE_ARCHIVE="${PVM_CANDIDATE_ARCHIVE:-/opt/akernel-pvm-ci/firecracker.tgz}"
fi
export FIRECRACKER_KERNEL_PROFILE=pvm AKERNEL_TARGETARCH=amd64
export AKERNEL_ENABLE_KATA=false AKERNEL_ENABLE_FIRECRACKER=true AKERNEL_ENABLE_RUNC=false
export AKERNEL_BUILD_PROXY=true DOCKER_BUILDKIT=1
export IMAGE="pvm-ci/all-in-one:${GITHUB_SHA:-local}"
deploy/scripts/build-image.sh --repository pvm-ci/all-in-one --tag "${GITHUB_SHA:-local}" > "${out}/image-build.log" 2>&1
mkdir -p "${out}/payload"
container=$(docker create --network none --entrypoint /bin/true "$IMAGE")
for file in manifest.json kernel.config vmlinux initrd.img; do
  docker cp "${container}:/opt/firecracker/${file}" "${out}/payload/${file}"
done
docker cp "${container}:/usr/local/bin/firecracker" "${out}/payload/firecracker"
docker rm "${container}" >/dev/null
python3 - "$out/payload" "$root/src/sandboxd/third_party/firecracker-pvm.json" <<'PAYLOAD'
import hashlib,json,sys
from pathlib import Path
r=Path(sys.argv[1]);pin=json.loads(Path(sys.argv[2]).read_text());manifest=json.loads((r/'manifest.json').read_text())
assert manifest['kernel_profile']=='pvm' and manifest['commit']==pin['commit'] and manifest['workflow_run_id']==str(pin['run_id'])
for name,section,key in [('firecracker','vmm','binary_sha256'),('vmlinux','guest_kernel','image_sha256'),('kernel.config','guest_kernel','config_sha256')]:
 assert hashlib.sha256((r/name).read_bytes()).hexdigest()==manifest[section][key]
(r/'image-payload-proof.json').write_text(json.dumps({'candidate_archive_sha256':pin['archive_sha256'],'image_id':'recorded separately','initrd_sha256':hashlib.sha256((r/'initrd.img').read_bytes()).hexdigest()},indent=2))
PAYLOAD
docker image inspect --format '{{.Id}}' "$IMAGE" > "${out}/image-id.txt"
export AKERNEL_FIRECRACKER_BACKEND=pvm AKERNEL_CHUNK_DB_SIZE=1GiB
export AKERNEL_CONTAINER_PROXY=http://10.0.2.2:3128
export AKERNEL_CONTAINER_NO_PROXY=localhost,127.0.0.1,172.17.0.1,10.88.0.1
# Keep Redis AOF/control writes on the L1 root disk while snapshot writes use XFS.
export AKERNEL_STANDALONE_DATA_DIR="/root/pvm-ci/adx-${GITHUB_RUN_ID:-local}"
storage="/audit-xfs/pvm-ci/adx-${GITHUB_RUN_ID:-local}"
mkdir -p "${storage}/filestore" "${storage}/checkpoints" \
  "${AKERNEL_STANDALONE_DATA_DIR}/filestore" "${AKERNEL_STANDALONE_DATA_DIR}/adx/checkpoints"
[[ $(findmnt -no FSTYPE -T "${AKERNEL_STANDALONE_DATA_DIR}") == ext4 ]]
for name in filestore checkpoints; do
  target="${AKERNEL_STANDALONE_DATA_DIR}/filestore"
  if [[ "${name}" == checkpoints ]]; then target="${AKERNEL_STANDALONE_DATA_DIR}/adx/checkpoints"; fi
  ! mountpoint -q "${target}" || { echo 'Private storage target already mounted' >&2; exit 1; }
done
python3 - "$out" "$AKERNEL_STANDALONE_DATA_DIR" "$storage" <<'STORAGE'
import json,sys
from pathlib import Path
out,root,storage=map(Path,sys.argv[1:])
(out/'storage-layout.json').write_text(json.dumps({'control_fstype':'ext4','runtime_fstype':'xfs','mounts':[{'source':str(storage/'filestore'),'target':str(root/'filestore')},{'source':str(storage/'checkpoints'),'target':str(root/'adx/checkpoints')}]},indent=2))
STORAGE
for name in filestore checkpoints; do
  target="${AKERNEL_STANDALONE_DATA_DIR}/filestore"
  if [[ "${name}" == checkpoints ]]; then target="${AKERNEL_STANDALONE_DATA_DIR}/adx/checkpoints"; fi
  mount --bind "${storage}/${name}" "${target}"
done
[[ $(findmnt -no FSTYPE -T "${AKERNEL_STANDALONE_DATA_DIR}/filestore") == xfs ]]
export AKERNEL_CONTROL_BIND=127.0.0.1 AKERNEL_DATA_BIND=127.0.0.1
python3 - "${root}/deploy/standalone/config/registry.json" <<'REGISTRY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);config=json.loads(p.read_text());config['registry']['scheme']='http';p.write_text(json.dumps(config))
REGISTRY
fixtures="${PVM_CI_FIXTURES:-/opt/akernel-pvm-ci/fixtures.json}"
[[ -f "${fixtures}" ]] || { echo 'PVM CI OCI/Nydus fixture manifest is missing' >&2; exit 1; }
registry=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["registry_container"])' "$fixtures")
[[ "${registry}" == review-registry ]]
docker start "${registry}" >/dev/null
mkdir -p "${out}/www"
printf 'PVM_PRIVATE_NETWORK_OK\n' > "${out}/www/health.txt"
if ip -4 addr show dev lo | grep -q '192.0.2.44/32'; then
  echo 'Private DNS fixture address already exists' >&2; exit 1
fi
ip addr add 192.0.2.44/32 dev lo
printf '192.0.2.44/32 lo\n' > "${out}/private-http-ip.txt"
python3 -m http.server 18090 --bind 0.0.0.0 --directory "${out}/www" > "${out}/private-http.log" 2>&1 &
printf '%s\n' "$!" > "${out}/private-http.pid"
mkdir -p "${AKERNEL_STANDALONE_DATA_DIR}/sandboxd/config"
printf 'nameserver 172.17.0.1\n' > "${AKERNEL_STANDALONE_DATA_DIR}/sandboxd/config/pvm-ci-resolv.conf"
python3 - "${root}/deploy/standalone/config/sandboxd_config.toml" <<'RESOLVER'
import sys
from pathlib import Path
p=Path(sys.argv[1]);text=p.read_text();old='resolv_conf_path="/etc/resolv.conf"'
assert text.count(old)==1
text=text.replace(old,'resolv_conf_path="/home/akernel/sandboxd/config/pvm-ci-resolv.conf"')
text=text.replace('filestore_dir_size="50G"','filestore_dir_size=""')
text=text.replace('filestore_xfs_enabled=false','filestore_xfs_enabled=true')
# This dedicated PVM qualification does not initialize unrelated runtimes.
text='\n'.join(line for line in text.splitlines() if not line.startswith('runsc='))+'\n'
p.write_text(text)
RESOLVER
python3 deploy/pvm/dns-fixture.py > "${out}/dns-fixture.log" 2>&1 &
printf '%s\n' "$!" > "${out}/dns-fixture.pid"
bash deploy/standalone/start.sh > "${out}/standalone-start.log" 2>&1
export AKERNEL_TEST_NETWORK_BLOCKED_HOST=blocked.pvm-ci.test
export AKERNEL_TEST_NETWORK_ALLOWED_HOST=allowed.pvm-ci.test
export AKERNEL_TEST_NETWORK_WILDCARD_HOST=child.blocked.pvm-ci.test
export AKERNEL_TEST_NETWORK_PORT=18090
source "${AKERNEL_STANDALONE_DATA_DIR}/sdk.env"
[[ -s "${AKERNEL_STANDALONE_DATA_DIR}/token" ]]
export AKERNEL_TOKEN="$(<"${AKERNEL_STANDALONE_DATA_DIR}/token")"
printf '::add-mask::%s\n' "${AKERNEL_TOKEN}"
[[ -n "${AKERNEL_SERVER_ADDRESS}" && -n "${AKERNEL_TOKEN}" ]]
export AKERNEL_RUN_INTEGRATION=1 AKERNEL_TEST_RUNTIME=firecracker-pvm
export AKERNEL_TEST_CHECKPOINT_MEMORY_MIB=2048
python3 - "$out" <<'SETTINGS'
import json,sys
from pathlib import Path
(Path(sys.argv[1])/'qualification-settings.json').write_text(json.dumps({'runtime':'firecracker-pvm','checkpoint_memory_mib':2048,'other_sdk_memory_mib':2048,'checkpoint_snapshot_type':'Full (requested explicitly by ADX)','debugctl_applied':False,'dedicated_pvm_profile':True},indent=2))
SETTINGS
export PYTHONPATH="${root}/sdk/python"
export AKERNEL_TEST_IMAGE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["oci"])' "$fixtures")
export AKERNEL_TEST_HTTP_IMAGE="$AKERNEL_TEST_IMAGE"
export AKERNEL_TEST_NETWORK_URL=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["network_url"])' "$fixtures")
cd sdk/python
qualification_failed=0
if ! python -m tests.e2e.run --level standalone --output "${out}/adx-standalone" --budget-seconds 7200; then qualification_failed=1; fi
if ! python3 - "$out/adx-standalone/summary.json" <<'COVERAGE'
import json,sys
summary=json.load(open(sys.argv[1]))
assert summary['results'] and all(r['status']=='passed' and not r.get('skipped_tests') for r in summary['results']), [(r['name'],r['status']) for r in summary['results']]
COVERAGE
then qualification_failed=1; fi
nydus=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["nydus"])' "$fixtures")
if ! AKERNEL_TEST_IMAGE="$nydus" AKERNEL_TEST_INTEGRATION_IMAGE="$nydus" \
  python -m tests.e2e.run --level standalone --case runtime-integration \
    --output "${out}/adx-nydus" --budget-seconds 1800; then qualification_failed=1; fi
if ! python3 - "$out/adx-nydus/summary.json" <<'NYDUS_COVERAGE'
import json,sys
summary=json.load(open(sys.argv[1]));assert summary['results'] and all(r['status']=='passed' and not r.get('skipped_tests') for r in summary['results'])
NYDUS_COVERAGE
then qualification_failed=1; fi
for kind in oci nydus; do
  image=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$fixtures" "$kind")
  if ! python benchmarks/sandbox_pressure.py --runtime firecracker-pvm --image "$image" \
    --processes 2 --threads 2 --duration 60 --cpu 500 --cpu-limit 0 \
    --memory 512 --mem-limit 0 --output "${out}/pressure-${kind}.json"; then qualification_failed=1; fi
done

if ! python3 - "$out" <<'PRESSURE_COVERAGE'
import json,sys
from pathlib import Path
for kind in ('oci','nydus'):
 report=json.loads((Path(sys.argv[1])/('pressure-'+kind+'.json')).read_text())
 assert report['status']=='passed' and report['succeeded']>0 and report['failed']==0, report
 assert report['succeeded']==report['attempted']
PRESSURE_COVERAGE
then qualification_failed=1; fi
# Both control-plane and runtime inventories must drain before module teardown.
for attempt in {1..30}; do
  python -m akernel_sdk.cli list --quiet > "${out}/control-inventory.txt"
  docker exec akernel-node sbox list > "${out}/runtime-inventory.txt"
  if [[ ! -s "${out}/control-inventory.txt" ]] && \
    [[ $(wc -l < "${out}/runtime-inventory.txt") == 1 ]] && ! pgrep -x firecracker; then
    break
  fi
  if [[ "${attempt}" == 30 ]]; then echo 'Agent DX inventory did not drain' >&2; exit 1; fi
  sleep 1
done
dmesg > "${out}/l1-dmesg.log"
if grep -E 'BUG:|Oops|WARNING:|general protection' "${out}/l1-dmesg.log"; then exit 1; fi

exit "${qualification_failed}"
