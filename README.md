# AKernel: Programmable Datacenter-Scale Infrastructure for Agents

## Overview

**AKernel** (**A**gent **Kernel**) is a distributed kernel that combines the performance of [AFaaS](https://www.usenix.org/conference/osdi25/presentation/chai-xiaohu) with the architecture of [openYuanrong](https://docs.openyuanrong.org/en/latest/index.html), enabling **true "datacenter use"** — treating the entire datacenter as a programmable extension of your AI Agent.

Traditional infrastructure tools (IaC, Kubernetes-native platforms, multi-cloud Terraform, and vendor-specific CDKs) are designed for provisioning infrastructure, not operating it. They fall short for AI agents, RL training, and data pipelines that require runtime elasticity, dynamic workflows, and programmatic access to datacenter capabilities.

AKernel solves this with five key advantages:

### Datacenter Use: Unified Programming Interface

A single Python SDK (`akernel_sdk`) to programmatically control compute, networking, and storage — no YAML, no manual orchestration. Multi-language support is planned.

```python
from akernel_sdk import Sandbox

with Sandbox(cpu=2000, memory=4096) as sb:
    result = sb.commands.run("echo 'hello from AKernel'")
    print(result.stdout)
```

### One-Click Deployment: From Laptop to Multi-Cloud

One all-in-one image, multiple deployment targets — deploy in under 10 minutes:

| Mode | Target | Scale | Deploy Time |
|------|--------|-------|-------------|
| **Standalone** | Single machine | 1 node | ~1 min |
| **Private K8s** | On-premise cluster | 100 nodes | ~5 min |
| **Multi-Cloud** | Alibaba Cloud, Huawei Cloud, etc. | 100 nodes | ~10 min |

### Secure Isolation with Extreme Performance

- **40 ms cold start\***: Fork-based launch with lazy loading for near-zero startup latency
- **Sandbox isolation**: [gVisor](https://github.com/google/gvisor) by
  default, with [Kata Containers](https://github.com/kata-containers/kata-containers)
  and [Firecracker](https://github.com/firecracker-microvm/firecracker)
  available on KVM-capable nodes
- **Checkpoint/Restore\***: Save and restore sandbox state for fast recovery

\* Planned for an open-source release and not available in AKernel v0.1.0.

### AI-Native Development and Operations

- **AI-contributed codebase**: Significant portions of AKernel code are authored by AI, enabling rapid iteration
- **AI-driven operations**: Cluster deployment, day-2 operations, and troubleshooting powered by AI agents
- **Agent-friendly**: Built as infrastructure that AI agents can programmatically control and reason about

### Full-Stack Observability

Built-in OpenTelemetry (OTEL) integration provides complete observability out of the box — not just resource management, but the full picture:

- **Metrics**: Prometheus-based collection for compute, networking, and sandbox performance
- **Dashboards**: Pre-configured Grafana dashboards for real-time cluster monitoring
- **Tracing & Logging**: End-to-end request tracing and centralized log aggregation

## Quick Start

### Quick Navigation

- 💡 [Examples](./sdk/python/examples/) - AKernel SDK examples and use cases
- 🏗️ [Architecture](#architecture) - System design and components
- 🚀 [Deployment](./deploy/README.md) - Installation and configuration guide

### Bootstrap a Cluster

AKernel provides guided Terraform deployment for Alibaba Cloud ACK and Huawei Cloud CCE. Clone the repository and prepare the cloud credentials before selecting one of the image options below.

The workflow requires Terraform, Docker, Helm, kubectl, Python 3, GNU Make, and cloud credentials with permission to create the required infrastructure. For Alibaba Cloud, export the credentials and region first:

```bash
git clone --recurse-submodules https://github.com/akernel-dev/akernel.git
cd akernel

export ALICLOUD_ACCESS_KEY="<your-access-key-id>"
export ALICLOUD_SECRET_KEY="<your-access-key-secret>"
export ALICLOUD_REGION="cn-hangzhou"
```

#### Option 1: Use the Official Image

Use the public AKernel image from Docker Hub:

```bash
make config VENDOR=aliyun \
  IMAGE_REPOSITORY=akerneldev/all-in-one \
  IMAGE_TAG=latest
make deploy
```

#### Option 2: Build from Source

Configure a registry that your cluster can access, then build and push the all-in-one image before deployment:

```bash
make config VENDOR=aliyun \
  IMAGE_REPOSITORY=registry.example.com/akernel/all-in-one \
  IMAGE_TAG=your-release-tag
docker login registry.example.com
make build
make push
make deploy
```

See the [Deployment Guide](./deploy/README.md) for prerequisites, cloud-specific configuration, deployment verification, and cluster cleanup, and the [Build Guide](./CLAUDE.md) for development details.


### Create a Sandbox

Install the Python SDK. The default installation includes the
`openyuanrong-sandbox` backend:

```bash
# PyPI
python -m pip install akernel-sdk

# Source
python -m pip install ./sdk/python

# Also install the actor backend
python -m pip install "akernel-sdk[openyuanrong-sdk]"
```

When the actor extra is installed, both backend packages are present and
`openyuanrong-sandbox` remains the automatic default. Set
`AKERNEL_BACKEND=openyuanrong-sdk` before importing `akernel_sdk` to select
the actor backend:

```bash
export AKERNEL_BACKEND=openyuanrong-sdk
```

When `openyuanrong-sdk` is used from a YuanRong function, the SDK process
inherits runtime paths configured by `builder/scripts/entryfile.sh`. That
entrypoint exports `PYTHONPATH` and, in some runtime layouts,
`LD_LIBRARY_PATH`; both variables are inherited by the application and its
child processes. `PYTHONPATH` prepends the runtime site-packages directory and
can change import resolution or shadow application dependencies.
`LD_LIBRARY_PATH` prepends runtime library directories and can change native
library resolution, causing ABI or version conflicts.

Configure the AKernel environment:

```bash
export AKERNEL_SERVER_ADDRESS="<your-akernel-server-address>"
export AKERNEL_TOKEN="<your-akernel-token>"
```

Use the SDK to create and interact with a sandbox:

```python
from akernel_sdk import Sandbox

with Sandbox(cpu=1000, memory=2048) as sandbox:
    result = sandbox.commands.run("echo 'hello from AKernel'")
    print(result.stdout)

    sandbox.files.write("/tmp/hello.txt", "hello from the SDK")
    print(sandbox.files.read("/tmp/hello.txt"))
```

Experimental gVisor sandboxes can request an exact NVIDIA GPU model:

```python
with Sandbox(xpu="gpu:l20:1") as sandbox:
    print(sandbox.commands.run("nvidia-smi -L").stdout)
```

GPU sandboxes require a compatible NVIDIA node and the gVisor `runsc`
runtime. `storage_mb` is measured in MiB and is supported by `runsc` and
Firecracker.

See the complete [basic usage example](./sdk/python/examples/basic_usage.py), the [sandbox runtime example](./sdk/python/examples/sandbox_runtime.py), and the other [SDK examples](./sdk/python/examples/) for more operations.

## Architecture

![AKernel architecture](./assets/akernel-architecture.svg)

### System Components

**Node-Level Infrastructure**
- **Sandbox runtimes**: gVisor by default; Kata Containers and Firecracker on
  KVM-capable nodes; and an explicitly enabled native Linux runc backend
- **sandboxd**: Sandbox lifecycle daemon with pluggable sandbox runtime integration
- **distill-fs**: Rust-based FUSE filesystem for lazy rootfs access, chunk caching, and deduplication

**Cluster-Wide Services**
- **Distributed Scheduler**: Workload-aware placement and scaling
- **API Gateway**: Unified interface for all operations
- **Object Storage**: Raw and Nydus rootfs images and read-only sandbox mounts
- **Cloud Provisioning**: Terraform modules for Alibaba Cloud ACK and Huawei Cloud CCE

### How It Works

1. **Agent Submits Workload**: Through unified API or SDK
2. **Scheduler Places Sandbox**: Selects a worker based on requested CPU,
   memory, storage, accelerator model, and available capacity
3. **Sandbox Created**: Prepares the rootfs and network and starts the selected sandbox runtime on the worker
4. **Workload Executes**: In secure, isolated sandboxes
5. **Resources Recycled**: Deletes the sandbox and returns its capacity to the cluster

## Roadmap

- [x] Kata Containers runtime on KVM-capable nodes
- [x] Firecracker microVM runtime on KVM-capable nodes
- [x] Optional native Linux runc runtime
- [x] Sandbox network ACL
- [ ] Fork-based sandbox launch based on gVisor
- [ ] Sandbox checkpoint and restore
- [ ] Support for GKE and AWS
- [x] Cgroup v2 node support

## License

AKernel is licensed under the [Apache License 2.0](./LICENSE).
