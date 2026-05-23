# Edge Kubernetes and Special Distributions

Vanilla Kubernetes assumes things the edge does not give it: 8 GiB of RAM per node, an SSD, a fat network pipe to the apiserver, and a humans on-call who can SSH in when something breaks. None of those assumptions hold in a retail back-of-store closet, a wind turbine nacelle, a cell tower at the bottom of a mountain pass, or a Raspberry Pi on a factory floor. This chapter covers the distributions that strip Kubernetes down to fit those environments, the architectural pattern that runs a central control plane and disperses agents to the edge, and the IoT-flavored projects (KubeEdge, Akri) that map K8s primitives onto sensors, cameras, and PLCs.

We assume you have read **chapter 03** (architecture) for the upstream component model, **chapter 26** (multi-cluster) for the hub-and-spoke topology that manages edge fleets, **chapter 19** (CSI/storage) for why dynamic provisioning is a cloud luxury, and **chapter 04** (etcd) for why sqlite-as-backend is more sensible than it sounds.

---

## Table of Contents

1. [TL;DR](#1-tldr)
2. [Why Vanilla Kubernetes Does Not Fit the Edge](#2-why-vanilla-kubernetes-does-not-fit-the-edge)
3. [The Two Architectural Patterns](#3-the-two-architectural-patterns)
4. [K3s: A Kubernetes That Fits in 60 MB](#4-k3s-a-kubernetes-that-fits-in-60-mb)
5. [K3s in 30 Seconds: The Install Moment](#5-k3s-in-30-seconds-the-install-moment)
6. [K3s Topologies by Scale](#6-k3s-topologies-by-scale)
7. [MicroK8s: Snap-Packaged Kubernetes](#7-microk8s-snap-packaged-kubernetes)
8. [k0s: The Zero-Friction Single Binary](#8-k0s-the-zero-friction-single-binary)
9. [Talos Linux: The OS That Is Only Kubernetes](#9-talos-linux-the-os-that-is-only-kubernetes)
10. [Distribution Comparison Matrix](#10-distribution-comparison-matrix)
11. [Embedded Cluster vs Centralized Control Plane](#11-embedded-cluster-vs-centralized-control-plane)
12. [KubeEdge Architecture](#12-kubeedge-architecture)
13. [KubeEdge Device CRDs: K8s for IoT](#13-kubeedge-device-crds-k8s-for-iot)
14. [EdgeMesh: East-West Networking Across the Boundary](#14-edgemesh-east-west-networking-across-the-boundary)
15. [OpenYurt: The Least Invasive Edge Approach](#15-openyurt-the-least-invasive-edge-approach)
16. [Akri: Device Discovery and Brokers](#16-akri-device-discovery-and-brokers)
17. [Edge Networking Concerns](#17-edge-networking-concerns)
18. [Edge Security](#18-edge-security)
19. [OTA Updates at the Edge](#19-ota-updates-at-the-edge)
20. [Edge Storage](#20-edge-storage)
21. [Edge Use Cases](#21-edge-use-cases)
22. [Cluster-as-a-Service for Edge](#22-cluster-as-a-service-for-edge)
23. [Edge-Specific Kubelet Tunings](#23-edge-specific-kubelet-tunings)
24. [The Single-Node Cluster](#24-the-single-node-cluster)
25. [HA at the Edge](#25-ha-at-the-edge)
26. [Cloud-Burst from the Edge](#26-cloud-burst-from-the-edge)
27. [Operating an Edge Fleet at Scale](#27-operating-an-edge-fleet-at-scale)
28. [What Edge K8s Gives Up](#28-what-edge-k8s-gives-up)
29. [Bare-Metal Kubernetes](#29-bare-metal-kubernetes)
30. [Migration Patterns](#30-migration-patterns)
31. [Pitfalls](#31-pitfalls)
32. [Closing Thoughts](#32-closing-thoughts)

---

## 1. TL;DR

- **Vanilla K8s is too fat for the edge.** A full kube-apiserver + etcd + controller-manager + scheduler footprint is 1–2 GiB of RAM and several hundred MB of disk before any workload starts. Edge nodes (Raspberry Pi, store gateway, in-vehicle compute) cannot afford it.
- **Two architectural responses exist.** Either run a full but slimmed cluster *at* the edge (K3s, MicroK8s, k0s, Talos), or run one cloud control plane and ship lightweight agents to the edge (KubeEdge, OpenYurt). The first gives offline tolerance and locality; the second simplifies fleet-wide policy.
- **K3s is the dominant edge distribution.** Single static binary (~60 MB), sqlite by default, embedded containerd/Flannel/Traefik, two roles (`server`, `agent`). Install in one curl. Scale from a Pi to a 100-store fleet via Rancher Fleet (ch 26).
- **MicroK8s** is a snap from Canonical, addon-driven, fine for dev laptops and small Ubuntu Core boxes. **k0s** is a single binary that pollutes nothing outside `/var/lib/k0s`. **Talos** is a Linux distribution with no shell, no SSH, no package manager — just kernel, machined, and kubelet, driven by `talosctl`.
- **KubeEdge** splits the control plane: CloudCore runs in the cloud, EdgeCore (edged + edgehub + metamanager + eventbus + devicetwin) runs on the edge, a persistent WebSocket connects them. Pod manifests are cached locally in metamanager so the edge keeps running when the link drops.
- **OpenYurt** is the least invasive: it adds `yurthub` as a local apiserver proxy on each edge node, caches responses, and gives offline tolerance to nodes joined to an otherwise-vanilla cluster.
- **Akri** discovers IoT devices (cameras via ONVIF, industrial via OPC UA, USB via udev) and schedules broker pods for each. It is the missing "device plugin for the network" of K8s.
- **The edge fleet operating model is GitOps.** A cluster per site, ArgoCD or Flux ApplicationSets parameterized by site, signed OTA updates, centralized observability, and bootstrap automation (PXE, cloud-init, Talos image).
- **The edge takes away cloud LBs, cloud CSI, cloud DNS, cloud IAM.** You replace each with a self-hosted analog: MetalLB, local-path/OpenEBS, ExternalDNS or static IPs, OIDC against a central IdP.

---

## 2. Why Vanilla Kubernetes Does Not Fit the Edge

Upstream Kubernetes is engineered for a datacenter. It assumes the control plane has multiple gigabytes of memory, etcd has flash storage, and worker nodes have a 1 Gbps link to the apiserver. None of that holds at the edge.

### 2.1 Memory Footprint

A baseline kubeadm-installed control plane consumes roughly:

```
┌───────────────────────────────────────────────────────────────────┐
│  VANILLA CONTROL PLANE — RESIDENT MEMORY (idle cluster)           │
├───────────────────────────────────────────────────────────────────┤
│  kube-apiserver         ............................  500–800 MB │
│  etcd                   ............................  300–500 MB │
│  kube-controller-manager............................  200–300 MB │
│  kube-scheduler         ............................  100–150 MB │
│  cloud-controller-manager...........................  100–150 MB │
│  kubelet + containerd   ............................  150–200 MB │
│  CoreDNS                ............................   50–100 MB │
│  kube-proxy             ............................   30–50  MB │
│  CNI agent (calico/cilium)..........................  100–300 MB │
│  ───────────────────────────────────────────────────────────────  │
│  TOTAL CONTROL PLANE NODE ..........................  1.5–2.5 GB │
└───────────────────────────────────────────────────────────────────┘
```

An edge device — a Raspberry Pi 4 with 4 GB of RAM, an Intel NUC-style retail gateway with 8 GB, a NVIDIA Jetson with 4 GB — cannot afford 1.5 GB for control-plane idle. K3s collapses that to about **150 MB** by compiling everything into one binary, throwing the in-tree cloud providers and storage drivers out, and replacing etcd with sqlite.

### 2.2 Disk Footprint

Vanilla K8s on disk:

```
/var/lib/etcd/         ~200 MB (cluster state, grows with objects)
/var/lib/containerd/   ~1–5 GB (image layers)
/var/lib/kubelet/      ~200 MB (pod logs, volumes)
/etc/kubernetes/       ~50 MB  (certs, kubeconfigs, manifests)
binaries               ~500 MB (kube-apiserver, etc.)
                       ───────
                       ~2–6 GB just to run the control plane
```

Edge devices commonly have a 16 GB SD card or a 32 GB eMMC. Half of that is system; the rest must hold images **and** the cluster runtime **and** application data. K3s ships as one binary; container images live in a deduplicated content store (`/var/lib/rancher/k3s/agent/containerd`); sqlite replaces etcd's MVCC files.

### 2.3 Network: Intermittently Connected

Datacenter K8s assumes a worker can reach the apiserver in <1 ms with effectively infinite uptime. Edge networks are the opposite:

- **Retail**: store-level DSL, 50 Mbps down, drops nightly for ISP maintenance.
- **Industrial**: WiFi behind machinery, 802.11 collision storms, RF noise.
- **Maritime/oil**: satellite link, 500–1500 ms latency, frequent total outage.
- **Automotive**: cellular, handover-induced drops, dead zones.
- **Telco MEC**: backhaul to core, very fast but capacity-shared.

A vanilla kubelet that loses its apiserver connection will eventually mark the node as `NotReady`, evict pods, and stop updating status. At the edge that is wrong: the local workload should keep running. The edge distributions (KubeEdge, OpenYurt) explicitly cache apiserver state locally so that the node "keeps doing what it was last told" through long disconnections.

### 2.4 Operations: No On-Site Admin

There is nobody at the wind turbine to type `kubectl` when a node misbehaves. Diagnostics must be remote, and recovery must be either automatic (a sister node takes over) or by replacing the entire device. This drives several design choices:

- Immutable host OS (Talos, Flatcar, Ubuntu Core) — you cannot "fix it" via SSH, only re-image it.
- API-driven host configuration (`talosctl apply-config`) — no shells.
- Watchdogs that hard-reboot a stuck node.
- Cluster bootstrap that needs *zero* keystrokes after the device powers on (PXE, cloud-init, factory provisioning).

### 2.5 Heterogeneous Hardware

Edge fleets mix:

- **CPU**: ARMv7, ARM64 (Cortex-A53/A72/A76, Neoverse), x86-64 (Atom, Xeon-D), RISC-V (emerging).
- **GPU**: NVIDIA Jetson (Tegra), AMD embedded, Intel Iris.
- **NPU**: Hailo, Google Coral Edge TPU, Rockchip NPU.
- **FPGA**: Xilinx Zynq, Intel Arria.
- **Sensors/devices**: ONVIF cameras, OPC UA PLCs, MQTT temperature sensors, CAN-bus buses, Modbus relays.

A control plane that hardcodes any of this — image arch, device plugin, kernel module — does not survive. The edge distributions tend to ship multi-arch images by default, and frameworks like **Akri** abstract heterogeneous devices behind a uniform CRD.

### 2.6 Long-Lived, Field-Updateable

A datacenter node lives 18 months and is replaced at the end of its lease. An edge node lives **5–10 years** in a wall enclosure. Over that lifetime it must survive:

- Kernel upgrades (security CVEs every few months).
- Container runtime upgrades.
- Kubernetes upgrades (one minor version per ~4 months upstream).
- Application upgrades (continuous).

Every one of these must happen over-the-air, signed, atomically (A/B partitions or transactional snapshots), and reversibly. Distros designed for this — Talos, Flatcar, Ubuntu Core, Bottlerocket — make A/B updates the default. K3s itself has a `system-upgrade-controller` that does in-cluster, declarative node upgrades.

---

## 3. The Two Architectural Patterns

Every edge K8s product picks one of two architectural patterns. Picking the right pattern is the most consequential choice you will make.

### 3.1 Distributed Control Plane at the Edge

```
                  ┌──────────────────────────┐
                  │   GitOps / Fleet Mgmt    │  (Rancher, ArgoCD)
                  │       (cloud)            │
                  └─────────────┬────────────┘
                                │ git sync, kube/HTTPS
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
 ┌─────────────┐          ┌─────────────┐         ┌─────────────┐
 │  Site A     │          │  Site B     │         │  Site C     │
 │             │          │             │         │             │
 │  K3s server │          │  K3s server │         │  K3s server │
 │  + etcd     │          │  + sqlite   │         │  3-node HA  │
 │  + workload │          │  + workload │         │  embed etcd │
 │             │          │             │         │             │
 │  apiserver  │          │  apiserver  │         │  apiserver  │
 │   (local)   │          │   (local)   │         │   (local)   │
 └─────────────┘          └─────────────┘         └─────────────┘
```

Every site has a **complete, autonomous cluster**. Workloads have a local apiserver, local scheduler, local kubelet. The cluster is fully functional offline — kubectl works, pods can be created, the scheduler can place. The fleet manager pushes desired state via Git or via the cluster's API; sites pull and reconcile.

**Pros**: total offline tolerance, low operation latency (pod start without round-trip to cloud), each site can use the full K8s API surface.

**Cons**: more compute overhead per site (you pay for a control plane), more sites to upgrade, etcd quorum cannot span sites.

This is the pattern of **K3s, MicroK8s, k0s, Talos**. It dominates retail, restaurant, hospitality, and factory edge.

### 3.2 Centralized Control Plane + Edge Agents

```
                ┌────────────────────────────┐
                │   Cloud Kubernetes Cluster  │
                │   (vanilla apiserver/etcd)  │
                │                              │
                │   + KubeEdge CloudCore       │
                │     - edgecontroller         │
                │     - devicecontroller       │
                │     - synccontroller         │
                │     - tunnel server          │
                └──────────────┬───────────────┘
                               │ persistent WebSocket
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
       ┌─────────┐        ┌─────────┐        ┌─────────┐
       │ Edge A  │        │ Edge B  │        │ Edge C  │
       │ EdgeCore│        │ EdgeCore│        │ EdgeCore│
       │  edged  │        │  edged  │        │  edged  │
       │  meta-  │        │  meta-  │        │  meta-  │
       │ manager │        │ manager │        │ manager │
       │ devtwin │        │ devtwin │        │ devtwin │
       └─────────┘        └─────────┘        └─────────┘
```

There is **one cloud apiserver** managing every edge node. The edge runs a lightweight agent that pretends to be a kubelet to the apiserver, but caches state locally so pods keep running through network loss. Devices are modeled as CRDs on the apiserver; the agent syncs them.

**Pros**: one control plane to upgrade, one place to apply policy, IoT device modeling, native cloud↔edge messaging.

**Cons**: when the link drops, no scheduling can happen at the edge (existing pods continue, but you cannot redeploy locally); apiserver scalability becomes the bottleneck (1000s of edge nodes in one apiserver); requires that *all* devices "register" with the cloud.

This is the pattern of **KubeEdge** and **OpenYurt**. It dominates telco MEC, IoT-heavy industrial deployments, and any case where the central operator wants exact visibility into every device.

### 3.3 Hybrid

Real-world deployments often mix. A retail company runs K3s in each store (autonomous) plus KubeEdge for the cameras and sensors *inside* the store (device-modeled). That works: K3s at the cluster level, KubeEdge as an addon for IoT inside.

---

## 4. K3s: A Kubernetes That Fits in 60 MB

K3s is the distribution that proved edge K8s could be **easier** than upstream, not just smaller. Started by Rancher (now SUSE), donated to CNCF as a sandbox project in 2020, it is the de facto standard for retail and per-site clusters.

Repository: `k3s-io/k3s`.

### 4.1 The Single Static Binary

K3s compiles **all** Kubernetes components — apiserver, controller-manager, scheduler, kubelet, kube-proxy — plus containerd, runc, Flannel, Traefik, CoreDNS, the local-path provisioner, and a service load balancer — into one Go binary called `k3s`. The binary is about 60 MB and is statically linked (no dependence on the system's libc beyond `nss`/`resolv`).

```
              ┌──────────────────────────────────┐
              │       /usr/local/bin/k3s         │
              │       (one static binary)        │
              │                                  │
              │  ┌────────────────────────────┐  │
              │  │  k3s server (CLI subcmd)   │  │
              │  │   ├── kube-apiserver       │  │
              │  │   ├── kube-controller-mgr  │  │
              │  │   ├── kube-scheduler       │  │
              │  │   ├── kine (sql shim)      │  │
              │  │   │      ↓                 │  │
              │  │   │   sqlite OR etcd OR    │  │
              │  │   │   postgres/mysql       │  │
              │  │   ├── kubelet              │  │
              │  │   ├── kube-proxy           │  │
              │  │   ├── containerd           │  │
              │  │   ├── flannel              │  │
              │  │   ├── coredns              │  │
              │  │   ├── traefik (ingress)    │  │
              │  │   ├── local-path           │  │
              │  │   └── klipper-lb (svcLB)   │  │
              │  └────────────────────────────┘  │
              └──────────────────────────────────┘
```

The "compile everything in" approach matters at the edge: there is one process to monitor, one binary to upgrade (atomic rename), one set of certificates. No "kubelet versus apiserver version skew" to debug, no missing CNI plugin file. Compare to kubeadm, where 10 packages have to be at compatible versions.

### 4.2 SQLite by Default; etcd or External DB Optional

K3s uses **kine** (k3s-io/kine) as a translation shim that lets the apiserver talk to a SQL store instead of etcd. Out of the box, kine writes to a single sqlite file at `/var/lib/rancher/k3s/server/db/state.db`.

```
┌──────────────────────┐      kine implements
│   kube-apiserver     │      the etcd gRPC API
│                      │  →   (Range, Put, Watch,
│  storage:            │      Compact, Txn, Lease)
│    etcd (over grpc)  │
└──────────┬───────────┘
           │ etcd gRPC API
           ▼
       ┌───────┐
       │ kine  │  (translates to SQL)
       └───┬───┘
           │
           ▼
  ┌────────────────────┐
  │  sqlite (default)  │
  │  or etcd           │
  │  or postgres       │
  │  or mysql/mariadb  │
  └────────────────────┘
```

For a single-node edge cluster, sqlite is **the right answer**: zero operational overhead, file-based snapshots, no leader election. For HA, you swap to one of:

1. **Embedded etcd** — three K3s servers form an etcd cluster among themselves. Same setup, same binary, just run three.
2. **External SQL** — Postgres or MySQL hosted somewhere reliable; servers are then stateless and can be scaled horizontally.
3. **External etcd** — for compatibility with existing infra.

**Pitfall**: sqlite stores a single sequential commit log. Disk-fsync latency on a slow SD card directly becomes apiserver latency. If you put a multi-tenant K3s on a cheap SD card you will see 200 ms+ apiserver responses. Use eMMC or SSD for any K3s with more than a handful of pods.

### 4.3 Embedded containerd

K3s does not depend on Docker. It ships containerd compiled in, configured with a tuned `config.toml`, and writes container state to `/var/lib/rancher/k3s/agent/containerd`. This saves the disk and RAM cost of dockerd (~150 MB resident) and removes a moving part.

You can override containerd with **CRI-O** if you set `--container-runtime-endpoint=unix:///var/run/crio/crio.sock` and disable the embedded one with `--disable-containerd`. Useful for sites standardized on CRI-O.

### 4.4 Embedded Flannel (VXLAN)

K3s ships Flannel as its default CNI with the VXLAN backend (default), or `wireguard-native`, or `host-gw` for L2-flat environments. Flannel is chosen for its simplicity: one Go daemon, one route table, no BGP, no operator. It does not do NetworkPolicy.

Override with any CNI by passing `--flannel-backend=none --disable-network-policy` to the server and then installing Cilium, Calico, or Antrea normally. This is the standard upgrade path once you outgrow Flannel.

### 4.5 Embedded Traefik (Ingress) and Klipper-LB

K3s ships **Traefik** as the default IngressController. Lightweight, well-documented, decent dashboard. Disable with `--disable=traefik` and install Nginx or Envoy Gateway instead.

K3s also ships **Klipper-LB** (also known as `svclb-`) — a service load balancer that runs a daemonset of `pod-per-node` that uses iptables to claim the LoadBalancer service's port on every node. This gives you `Service: type=LoadBalancer` without a cloud provider. For real BGP-announced VIPs you swap it for **MetalLB**.

### 4.6 What Is Removed

K3s explicitly drops:

- **In-tree cloud providers** (AWS/GCP/Azure CCMs) — not needed at the edge.
- **In-tree volume plugins** (the deprecated providers like `kubernetes.io/aws-ebs`) — replaced by CSI.
- **Alpha features** that bloat the API surface.
- **The legacy Docker CRI shim** — gone, containerd only.

This shrinks the binary and removes attack surface.

### 4.7 Two Roles: `server` and `agent`

K3s nodes have two roles:

- **`k3s server`** — runs the control plane *and* a kubelet. The first server initializes the cluster; additional servers join as etcd peers (if embedded etcd) or as more API serving replicas (if external DB).
- **`k3s agent`** — runs only the kubelet, kube-proxy, containerd, and Flannel agent. Pure worker.

```
          K3s Server #1                K3s Server #2          K3s Agent #1
   ┌───────────────────────┐    ┌───────────────────────┐    ┌───────────┐
   │ apiserver             │    │ apiserver             │    │           │
   │ controller-manager    │◄──►│ controller-manager    │    │ kubelet   │
   │ scheduler             │    │ scheduler             │    │ kube-proxy│
   │ embedded etcd peer    │◄──►│ embedded etcd peer    │    │ flannel   │
   │ kubelet               │    │ kubelet               │    │ containerd│
   │ kube-proxy            │    │ kube-proxy            │    │           │
   │ flannel/containerd    │    │ flannel/containerd    │    │           │
   └───────────────────────┘    └───────────────────────┘    └─────┬─────┘
                                                                    │
                                          k3s.yaml token + URL  ────┘
                                          to one of the servers
```

The token (`/var/lib/rancher/k3s/server/node-token` on a server) is the join secret. Distribute it via cloud-init / first-boot scripts.

### 4.8 HA Modes

| Mode | Servers | DB | Best for |
|------|--------|-----|----------|
| Single-server | 1 | sqlite | Lab, dev, single-Pi edge |
| External DB | 2–N | Postgres / MySQL / etcd | When you already have managed DB |
| Embedded etcd | 3 or 5 | etcd inside k3s | Production edge HA |

**Embedded etcd** is by far the most common production choice. Three K3s servers join with `--cluster-init` on the first and `--server https://first:6443 --token <T>` on the others. Etcd quorum among the three.

---

## 5. K3s in 30 Seconds: The Install Moment

The signature K3s experience — and the reason it took off — is the install pipeline. From `apt-get`-clean Ubuntu to a working cluster:

```bash
curl -sfL https://get.k3s.io | sh -
```

Thirty seconds later:

```bash
sudo k3s kubectl get nodes
# NAME       STATUS   ROLES                  AGE   VERSION
# pi-edge    Ready    control-plane,master   23s   v1.30.3+k3s1
```

Behind the scenes the installer:

1. Detects systemd vs OpenRC.
2. Downloads the right `k3s` binary for the architecture (`uname -m`).
3. Drops a `/usr/local/bin/k3s` and symlinks `kubectl`, `crictl`, `ctr`.
4. Creates a systemd unit `k3s.service`.
5. Starts it. The server self-bootstraps sqlite, generates certs, deploys CoreDNS / Traefik / local-path.

### 5.1 Real Install Commands

**Single node with sqlite:**

```bash
curl -sfL https://get.k3s.io | sh -
```

**Server with custom args** (disable Traefik, use Cilium later, write kubeconfig world-readable):

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --disable=traefik \
  --disable=servicelb \
  --flannel-backend=none \
  --disable-network-policy \
  --write-kubeconfig-mode=644 \
  --node-label edge.example.com/site=store-042 \
  --node-taint dedicated=edge:NoSchedule" sh -
```

**First server of a 3-node embedded-etcd HA cluster:**

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --cluster-init \
  --token <SHARED_TOKEN> \
  --tls-san=k3s.example.com \
  --node-name=store-042-srv1" sh -
```

**Joining the second/third server:**

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server \
  --server https://store-042-srv1:6443 \
  --token <SHARED_TOKEN> \
  --tls-san=k3s.example.com" sh -
```

**Adding an agent:**

```bash
curl -sfL https://get.k3s.io | K3S_URL=https://store-042-srv1:6443 \
  K3S_TOKEN=<SHARED_TOKEN> sh -
```

### 5.2 Air-Gapped Install

The edge often has no internet. K3s ships air-gap install bundles: a single tarball with the binary, the install script, and pre-loaded container images. You unpack to `/var/lib/rancher/k3s/agent/images/`, then run the installer with `INSTALL_K3S_SKIP_DOWNLOAD=true`.

```bash
# On a workstation with internet:
wget https://github.com/k3s-io/k3s/releases/download/v1.30.3+k3s1/k3s
wget https://github.com/k3s-io/k3s/releases/download/v1.30.3+k3s1/k3s-airgap-images-arm64.tar.zst
# Ship to the edge device (USB, signed package).
# On the edge:
sudo mkdir -p /var/lib/rancher/k3s/agent/images/
sudo cp k3s-airgap-images-arm64.tar.zst /var/lib/rancher/k3s/agent/images/
sudo install k3s /usr/local/bin/
sudo INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_EXEC="server" \
  ./install.sh
```

The simplicity moment — one binary, one tarball, one command — is what makes K3s viable in the field. There is no "and then yum install three packages and start two services" choreography that fails halfway through.

---

## 6. K3s Topologies by Scale

K3s scales from a single Pi to thousands of sites. The right topology changes by size.

### 6.1 Single-Node Lab / IoT Gateway

```
        ┌──────────────────────────┐
        │  Raspberry Pi 4 (8 GB)   │
        │                          │
        │  k3s server (sqlite)     │
        │  workload pods            │
        │  Klipper-LB               │
        └──────────────────────────┘
```

Single `k3s server`. No HA. Suitable for **home automation, IoT gateways, demos, dev clusters**. Backup the sqlite file periodically; node failure = restore from backup on a new device.

### 6.2 3-Node HA at a Site

```
        ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
        │ k3s server 1 │  │ k3s server 2 │  │ k3s server 3 │
        │ embed etcd   │◄►│ embed etcd   │◄►│ embed etcd   │
        │ workload     │  │ workload     │  │ workload     │
        └──────────────┘  └──────────────┘  └──────────────┘
                    ▲                    ▲
                    │                    │
        ┌───────────┴──────┐  ┌──────────┴───────┐
        │ k3s agent 1      │  │ k3s agent 2 ...  │
        └──────────────────┘  └──────────────────┘
```

Three servers running embedded etcd, optional agents. Suitable for a **store, factory floor, regional hub**. Failure of one server is tolerated.

Cross-failure-domain placement matters: 3 servers on the same shelf, same PDU, same UPS = not HA. Spread them across different racks, different power sources. We will revisit this in §25.

### 6.3 100-Site Fleet

```
                     ┌─────────────────────────────────┐
                     │  Cloud Management Cluster       │
                     │  (Rancher / Fleet / ArgoCD)     │
                     └─────────────────┬───────────────┘
                                       │
                                       │ git pull, kube/HTTPS
                                       ▼
              ┌──────────────┬──────────────┬───────...────┐
              │              │              │              │
         ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐   ┌───▼──────┐
         │ Site 001 │   │ Site 002 │   │ Site 003 │   │ Site 100 │
         │ 3xK3s HA │   │ 3xK3s HA │   │ 3xK3s HA │   │ 3xK3s HA │
         └──────────┘   └──────────┘   └──────────┘   └──────────┘
```

Each site is autonomous (its own apiserver). Rancher Fleet (or ArgoCD ApplicationSets) renders per-site YAML and pushes via Git. Sites pull, reconcile, report status back. See ch 26 for the management plane.

---

## 7. MicroK8s: Snap-Packaged Kubernetes

Repository: `canonical/microk8s`. MicroK8s is Canonical's edge/development distribution, distributed as a **snap** package — meaning it installs identically on any modern Linux, has a transactional update mechanism, and is sandboxed.

### 7.1 What MicroK8s Is

```
              ┌───────────────────────────────────┐
              │   sudo snap install microk8s      │
              │                                   │
              │   Snap package contains:          │
              │   - kubelite (similar to k3s:     │
              │     compiled-together server)     │
              │   - dqlite (distributed sqlite)   │
              │   - containerd                    │
              │   - calico/flannel                │
              │   - cni plugins                   │
              │                                   │
              │   Confined under /snap/microk8s   │
              │   and /var/snap/microk8s/         │
              └───────────────────────────────────┘
```

`kubelite` is MicroK8s's analog of the K3s single-binary approach: apiserver, controller-manager, scheduler, kubelet, kube-proxy in one process. **dqlite** (distributed sqlite, also from Canonical) replaces etcd for HA.

### 7.2 Addons System

Unique to MicroK8s: a curated addon catalog enabled with `microk8s enable <name>`.

```bash
sudo microk8s enable dns
sudo microk8s enable storage          # hostpath storage class
sudo microk8s enable ingress           # nginx ingress
sudo microk8s enable metallb:10.64.140.43-10.64.140.49
sudo microk8s enable observability     # kube-prometheus-stack
sudo microk8s enable cert-manager
sudo microk8s enable gpu               # NVIDIA device plugin
sudo microk8s enable kubeflow
```

Each addon is a small script that installs the relevant helm chart or YAML, version-pinned to the MicroK8s release. Saves you from writing the same boilerplate ten times.

### 7.3 Ubuntu and Ubuntu Core Integration

MicroK8s on **Ubuntu Core** (the immutable, all-snap Ubuntu) is one of the few "appliance-grade" K8s stacks: every component (kernel, snapd, microk8s, your app) is delivered as a snap with A/B transactional updates and signed channels. Hard to beat for fleet appliances if you are already in the Ubuntu ecosystem.

### 7.4 ARM Support

MicroK8s ships ARM64 (Raspberry Pi 4, NVIDIA Jetson) snaps as first-class. ARMv7 is supported but slower-moving.

### 7.5 Where MicroK8s Fits

- **Developer workstations** — fast to install, easy to nuke (`microk8s reset`).
- **Edge appliances on Ubuntu Core** — A/B update + signed snap channel.
- **CI runners** — disposable clusters with addons.

It is less common at very large fleet scale, where K3s tends to win due to its smaller dependency surface and explicit fleet tooling (Rancher Fleet).

### 7.6 The Auto-Refresh Footgun

By default, snap auto-refreshes every 4–6 hours. This means a production MicroK8s can spontaneously upgrade itself. You **must** pin the snap channel and defer refreshes:

```bash
sudo snap refresh --hold microk8s
# Or restrict to a specific channel:
sudo snap refresh microk8s --channel=1.30/stable
# Or schedule:
sudo snap set system refresh.hold="2030-01-01T00:00:00Z"
```

Forgetting this in production is a real pitfall — covered in §31.

---

## 8. k0s: The Zero-Friction Single Binary

Repository: `k0sproject/k0s`. From Mirantis. Similar philosophy to K3s — single static binary, batteries-included — with a few opinionated differences.

### 8.1 What Makes k0s Different

- **No host pollution**. Everything lives under `/var/lib/k0s/`. No `/etc/cni/`, no `/var/lib/kubelet/` polluted; k0s creates them inside its data directory.
- **Vanilla upstream components**. k0s does not patch Kubernetes; it ships unmodified upstream apiserver, kubelet, etc. compiled in. (K3s does patch a few things — notably the storage backend hook for kine.)
- **kube-router** as default CNI (with Calico/Cilium as alternatives), giving NetworkPolicy out of the box.
- **HA via external etcd or PostgreSQL** through kine, same as K3s. Embedded etcd as well.

### 8.2 k0sctl

`k0sctl` is the multi-node installer:

```yaml
apiVersion: k0sctl.k0sproject.io/v1beta1
kind: Cluster
metadata:
  name: edge-cluster-01
spec:
  hosts:
    - ssh:
        address: 10.0.1.10
        user: root
        keyPath: ~/.ssh/edge_key
      role: controller+worker
    - ssh:
        address: 10.0.1.11
        user: root
        keyPath: ~/.ssh/edge_key
      role: controller+worker
    - ssh:
        address: 10.0.1.12
        user: root
        keyPath: ~/.ssh/edge_key
      role: controller+worker
  k0s:
    version: 1.30.3+k0s.0
    config:
      spec:
        api:
          k0sApiPort: 9443
        storage:
          type: etcd
```

```bash
k0sctl apply --config k0sctl.yaml
```

Bootstraps the whole cluster over SSH, installs the binaries, joins them. Useful when you have provisioned bare-metal edge sites via PXE and want to layer k0s on top via Ansible-like flow.

### 8.3 The Worker Token Model

k0s has clear separation: controller nodes generate join tokens for workers. Tokens are short, signed JWTs.

```bash
sudo k0s token create --role=worker --expiry=1h
```

The token URL is then passed to `k0s install worker` on the edge node.

### 8.4 Where k0s Fits

Production bare-metal Kubernetes when you want **clean, vanilla upstream** packaged for ease of install. Less optimized for the very smallest edge devices (it does not bundle as aggressively as K3s) but excellent for "small datacenter" or "remote office" clusters.

---

## 9. Talos Linux: The OS That Is Only Kubernetes

Repository: `siderolabs/talos`. Talos is not a Kubernetes distribution — it is a **Linux distribution that exists only to run Kubernetes**. There is no shell, no SSH, no package manager, no init scripts. The userland is one Go binary (`machined`) and it accepts an API.

### 9.1 What Is Inside a Talos Node

```
                  ┌──────────────────────────────────┐
                  │            Talos Node             │
                  │                                   │
                  │  ┌─────────────────────────────┐ │
                  │  │ Linux Kernel (immutable)    │ │
                  │  └─────────────────────────────┘ │
                  │  ┌─────────────────────────────┐ │
                  │  │ machined (PID 1)            │ │
                  │  │  - exposes Talos gRPC API   │ │
                  │  │  - configures network/disks │ │
                  │  │  - drives kubelet           │ │
                  │  └─────────────────────────────┘ │
                  │  ┌─────────────────────────────┐ │
                  │  │ kubelet                      │ │
                  │  └─────────────────────────────┘ │
                  │  ┌─────────────────────────────┐ │
                  │  │ containerd                   │ │
                  │  └─────────────────────────────┘ │
                  │                                   │
                  │  rootfs: read-only squashfs       │
                  │  state:  ephemeral overlay        │
                  │  data:   /var/lib/etcd, etc.      │
                  └──────────────────────────────────┘
```

There is no `bash`, no `apt`, no `yum`, no `journalctl` you can SSH to. To do anything, you point `talosctl` at the node:

```bash
talosctl --talosconfig ./talosconfig --nodes 10.0.1.10 \
  apply-config --file controlplane.yaml

talosctl --nodes 10.0.1.10 services
talosctl --nodes 10.0.1.10 logs kubelet
talosctl --nodes 10.0.1.10 dmesg
talosctl --nodes 10.0.1.10 reboot
talosctl --nodes 10.0.1.10 upgrade --image ghcr.io/siderolabs/installer:v1.7.5
```

### 9.2 Machine Config

A Talos node is configured by a single YAML — the **machine config** — which is applied via the API. It describes the host (disks, network, time servers, certificates) **and** the Kubernetes role (controlplane vs worker).

```yaml
version: v1alpha1
machine:
  type: controlplane
  install:
    disk: /dev/nvme0n1
    image: ghcr.io/siderolabs/installer:v1.7.5
    wipe: false
  network:
    hostname: edge-cp-01
    interfaces:
      - interface: eth0
        addresses:
          - 10.0.1.10/24
        routes:
          - network: 0.0.0.0/0
            gateway: 10.0.1.1
  time:
    servers:
      - time.cloudflare.com
cluster:
  controlPlane:
    endpoint: https://10.0.1.10:6443
  clusterName: edge-fleet-042
  network:
    cni:
      name: cilium
    podSubnets:
      - 10.244.0.0/16
    serviceSubnets:
      - 10.96.0.0/12
```

### 9.3 The API-Driven Model

```
        ┌───────────────────────┐         ┌───────────────────────┐
        │   Operator workstation │         │   Talos Node           │
        │                        │         │                        │
        │   talosctl (CLI)       │  gRPC   │   machined (server)    │
        │   talosconfig (creds)  │ ──────► │   - reads machineconfig│
        │                        │ (mTLS)  │   - reconciles host    │
        │                        │         │   - manages kubelet    │
        └───────────────────────┘         └───────────────────────┘
```

Every operation — configuration, log retrieval, service restart, reboot, upgrade — flows through `talosctl` over a mutually authenticated gRPC connection. The Talos config bundle (`talosconfig`) is the equivalent of the kubeconfig but for the OS layer.

### 9.4 Immutable Root, Atomic Updates

Talos uses an A/B partition scheme. `talosctl upgrade` downloads a new system image, flashes the inactive partition, and reboots into it. If the new partition fails to come up healthy, the bootloader falls back to the previous one. Mass-fleet upgrades are then a matter of issuing the API call to each node in waves.

### 9.5 Why Talos Wins at Unattended Edge

- **No way for an operator to drift the config**: there is no shell to log into. Every change is a config diff.
- **Reduced attack surface**: no sudo, no PAM, no SSH, no setuid binaries.
- **Reproducible builds**: an image of v1.7.5 is bit-identical everywhere.
- **PXE / cloud-image friendly**: ship a single ISO, embed the machine config via metadata.

The trade-off is the learning curve — you cannot fix a broken Talos node by SSHing in and editing a file. You must reconfigure via API. For staff engineers building robust fleets, this is a feature.

### 9.6 Talos + K3s vs Talos + Vanilla

Talos *defaults* to vanilla upstream Kubernetes via kubeadm-style components. You **could** run K3s on top of Talos, but it is not the typical path — Talos's installer assumes upstream binaries. If you want "K3s simplicity plus immutable OS", choose Flatcar or Ubuntu Core + K3s.

---

## 10. Distribution Comparison Matrix

| Distribution | Footprint (RAM/disk) | HA backend | CNI | OS req | Best fit | Opinionated? |
|--------------|---------------------|------------|-----|--------|----------|--------------|
| **K3s** | ~150 MB / ~250 MB | sqlite / embed etcd / SQL | Flannel (swap) | Any modern Linux | Edge per-site, IoT, retail | Medium |
| **MicroK8s** | ~200 MB / ~700 MB | dqlite | Calico/Flannel | Any snap-capable Linux | Ubuntu shops, dev | High (addons) |
| **k0s** | ~200 MB / ~300 MB | embed etcd / SQL | kube-router (swap) | Any Linux, no host pollution | Bare-metal small cluster | Low |
| **Talos** | depends on K8s; OS ~120 MB | upstream etcd | any | Talos itself (immutable) | Unattended edge, secure fleets | Very high (no shell) |
| **kubeadm (vanilla)** | ~1.5 GB / ~2 GB | etcd | any | Any Linux | Datacenter; not edge | Low |
| **Rancher RKE2** | ~500 MB / ~1 GB | embed etcd / SQL | Canal (Calico+Flannel) | Any Linux | Government / FIPS edge | Medium |

### 10.1 Footprint Reality Check

The numbers above are control-plane-only, idle. Real workloads add their own memory. A K3s server on a 4 GB Pi typically leaves ~3 GB for workloads. Vanilla kubeadm on the same Pi would leave ~2 GB — *and* you would spend more on operating it.

### 10.2 Supported Topologies

| Distribution | Single-node | 3-node HA | Multi-region | Air-gap | ARM64 | ARMv7 |
|--------------|-------------|-----------|--------------|---------|-------|-------|
| K3s | yes | yes | via Rancher | yes | yes | yes |
| MicroK8s | yes | yes (dqlite) | via Juju | yes | yes | limited |
| k0s | yes | yes | via k0sctl | yes | yes | no |
| Talos | yes | yes | via Sidero | yes | yes | no |
| kubeadm | yes | yes | via Cluster API | yes | yes | limited |

### 10.3 Opinionated vs Flexible

K3s, MicroK8s, and Talos make many decisions for you (CNI, ingress, storage class). k0s is closer to vanilla — fewer defaults, more flexibility. Pick "opinionated" when you have many sites with similar needs; pick "flexible" when each site is bespoke.

---

## 11. Embedded Cluster vs Centralized Control Plane

This is the architectural decision that drives everything else. Let us work through it.

### 11.1 Embedded (K3s / MicroK8s / k0s / Talos)

**Pros**:
- Local apiserver → pod start latency is single-digit ms.
- Total offline tolerance — kubectl works during the link drop.
- Each site can use the full K8s API surface: CRDs, custom controllers, operators.
- Failures are isolated: one site crashing does not affect others.

**Cons**:
- Per-site overhead: every site pays ~200 MB RAM and 1–2 GB disk for the control plane.
- More cluster credentials to manage (one kubeconfig per site).
- More upgrades to orchestrate.
- etcd quorum must be within the site (you cannot stretch etcd across a 200 ms WAN).

### 11.2 Centralized (KubeEdge / OpenYurt)

**Pros**:
- One control plane to upgrade, patch, monitor.
- One pane of glass: `kubectl get nodes` shows every edge.
- Cross-site policy is trivial (it is the same apiserver).
- Lower per-site overhead — only an agent, ~50–100 MB.

**Cons**:
- When the cloud link drops, you cannot redeploy locally. Existing pods continue; new pods cannot land.
- Apiserver scalability is the limit (~5000 nodes per apiserver, fewer at the edge due to slow links).
- Device-level details (e.g., GPU model, sensor type) must be modeled centrally.
- A bad rollout from the cloud affects every site simultaneously.

### 11.3 The Heuristic

Pick **embedded** when:
- Sites must function for hours/days without the cloud.
- Sites have ≥4 GB RAM each.
- Site count is moderate (10s to 1000s).
- Per-site customization is needed.

Pick **centralized** when:
- Sites are very small (256 MB IoT gateway).
- Latency to cloud is OK (telco MEC inside a single ISP).
- Centralized device modeling is critical (camera fleets, sensor networks).
- Number of "sites" is huge (10,000+).

---

## 12. KubeEdge Architecture

Repository: `kubeedge/kubeedge`. Born at Huawei, incubating at CNCF. The defining edge-IoT project.

### 12.1 The Split: CloudCore and EdgeCore

```
                        ┌─────────────────────────────────┐
                        │       Cloud K8s Cluster          │
                        │                                  │
                        │  ┌────────────────────────────┐ │
                        │  │  kube-apiserver / etcd      │ │
                        │  └─────────────┬──────────────┘ │
                        │                │                 │
                        │  ┌─────────────▼──────────────┐ │
                        │  │  CloudCore                 │ │
                        │  │   ┌─────────────────┐      │ │
                        │  │   │ edgecontroller  │      │ │
                        │  │   │ devicecontroller│      │ │
                        │  │   │ synccontroller  │      │ │
                        │  │   │ tunnel server   │      │ │
                        │  │   └─────────────────┘      │ │
                        │  └─────────────┬──────────────┘ │
                        └────────────────┼─────────────────┘
                                         │
                                         │ WebSocket / QUIC
                                         │ (long-lived, mTLS)
                                         ▼
              ┌──────────────────────────────────────────┐
              │                Edge Node                  │
              │                                           │
              │   ┌─────────────────────────────────┐    │
              │   │           EdgeCore               │    │
              │   │  ┌─────────┐  ┌──────────────┐  │    │
              │   │  │ edged   │  │ edgehub      │  │    │
              │   │  │(kubelet)│◄►│ (tunnel cli) │  │    │
              │   │  └────┬────┘  └──────┬───────┘  │    │
              │   │       │              │           │    │
              │   │  ┌────▼────┐    ┌────▼────┐     │    │
              │   │  │ meta-   │    │event-   │     │    │
              │   │  │ manager │    │ bus     │     │    │
              │   │  │(SQLite) │    │ (MQTT)  │     │    │
              │   │  └────┬────┘    └────┬────┘     │    │
              │   │       │              │           │    │
              │   │  ┌────▼──────────────▼────┐     │    │
              │   │  │     devicetwin          │     │    │
              │   │  │  (desired / reported)   │     │    │
              │   │  └─────────────────────────┘     │    │
              │   └─────────────────────────────────┘    │
              │                  │                         │
              │            ┌─────▼─────┐                   │
              │            │  Devices  │  (cameras,       │
              │            │  via MQTT │   sensors, PLCs) │
              │            └───────────┘                   │
              └──────────────────────────────────────────┘
```

### 12.2 CloudCore Components

- **edgecontroller**: watches Pod, ConfigMap, Secret, Service, Endpoint, Node objects in the apiserver, filters those targeted to edge nodes (via `node selector` or the `node-role.kubernetes.io/edge` label), and pushes them down through the tunnel.
- **devicecontroller**: watches `Device` and `DeviceModel` CRDs and pushes them to the edge node hosting the device.
- **synccontroller**: persists what was pushed to the edge into a `ObjectSync`/`ClusterObjectSync` so reconnects can resync deltas instead of full state.
- **tunnel server**: terminates the WebSocket from EdgeCore; also supports `kubectl exec`/`logs` reverse proxying so cloud users can interact with edge pods.

### 12.3 EdgeCore Components

- **edged**: a stripped-down kubelet. Runs containers via CRI (containerd), reports pod status. Does **not** talk to the apiserver directly — talks to metamanager.
- **edgehub**: the WebSocket client. Reads from the tunnel, writes received objects into metamanager. Buffers outgoing messages when disconnected.
- **metamanager**: a local apiserver-stand-in. Stores Pods, ConfigMaps, etc. in a local SQLite. edged reads from it. **This is the key offline-tolerance trick** — even with the cloud unreachable, edged sees the same data it had at last sync.
- **eventbus**: an MQTT broker (Mosquitto or built-in) that talks to physical devices in the local subnet.
- **devicetwin**: maintains the **desired** and **reported** state for each device. The cloud writes desired; the device's MQTT messages update reported. Sync via edgehub when the link is up.

### 12.4 The WebSocket Tunnel

A single persistent WebSocket per edge node connects EdgeCore to CloudCore. Authentication is mTLS using a token-bootstrap flow:

```
Edge boot → keadm join --cloudcore-ipport=cloud.example.com:10000 \
              --token=<bootstrap token>
↓
Edge requests cert from CloudCore, presenting bootstrap token.
CloudCore signs an edge node certificate.
↓
Edge establishes mTLS WebSocket. From now on, all traffic flows here.
```

Loss of the WebSocket triggers retry-with-backoff in edgehub. Crucially, **pods keep running** because edged reads from metamanager, not from the WebSocket.

### 12.5 Reconnect Semantics

When the link returns:

1. edgehub re-establishes the WebSocket.
2. CloudCore's synccontroller computes the delta (objects changed since last `resourceVersion`).
3. edgehub receives the delta, writes to metamanager.
4. edged sees changes via metamanager watches, applies (creates / updates / deletes pods).
5. Reported pod statuses go up. Device twin "reported" deltas flush up.

This is **eventually consistent** — the edge can drift for hours or days, then converge on reconnect.

---

## 13. KubeEdge Device CRDs: K8s for IoT

KubeEdge's IoT story is built on two CRDs.

### 13.1 DeviceModel: The Schema

A `DeviceModel` describes the *kind* of device — its properties, types, units. Think of it as a schema or a class.

```yaml
apiVersion: devices.kubeedge.io/v1beta1
kind: DeviceModel
metadata:
  name: temperature-sensor-v1
  namespace: edge-default
spec:
  properties:
    - name: temperature
      description: Ambient temperature reading
      type: FLOAT
      accessMode: ReadOnly
      minimum: -40
      maximum: 85
      unit: Celsius
    - name: humidity
      description: Relative humidity
      type: FLOAT
      accessMode: ReadOnly
      minimum: 0
      maximum: 100
      unit: Percent
    - name: sampling-interval
      description: Sampling interval in seconds
      type: INT
      accessMode: ReadWrite
      defaultValue: 60
```

### 13.2 Device: The Instance

A `Device` is an instance of a `DeviceModel`, bound to an edge node, with a desired-state block and a reported-state block.

```yaml
apiVersion: devices.kubeedge.io/v1beta1
kind: Device
metadata:
  name: sensor-warehouse-01
  namespace: edge-default
  labels:
    location: warehouse-zone-A
spec:
  deviceModelRef:
    name: temperature-sensor-v1
  nodeName: edge-warehouse-gateway-01
  protocol:
    protocolName: mqtt
    configData:
      ip: 192.168.1.50
      port: 1883
      topic: sensors/warehouse/sensor-01
  properties:
    - name: sampling-interval
      desired:
        value: "30"
        metadata:
          type: INT
status:
  twins:
    - propertyName: temperature
      reported:
        value: "22.4"
        metadata:
          type: FLOAT
          timestamp: "2026-05-23T10:30:00Z"
    - propertyName: humidity
      reported:
        value: "45.2"
        metadata:
          type: FLOAT
          timestamp: "2026-05-23T10:30:00Z"
    - propertyName: sampling-interval
      reported:
        value: "30"
```

### 13.3 The Sync Flow

```
   Operator writes "sampling-interval=30" to Device spec  (kubectl apply)
              │
              ▼
   devicecontroller (CloudCore) sees the change, pushes via WebSocket
              │
              ▼
   edgehub receives, writes to metamanager
              │
              ▼
   devicetwin reads, builds an MQTT message:
       topic = "$hw/events/device/sensor-warehouse-01/twin/update/delta"
       payload = { sampling-interval: 30 }
              │
              ▼
   eventbus publishes via MQTT to the device firmware
              │
              ▼
   Device reads MQTT, applies new sampling interval, publishes ack:
       topic = "$hw/events/device/sensor-warehouse-01/twin/update"
       payload = { reported: { sampling-interval: 30 } }
              │
              ▼
   eventbus → devicetwin (updates reported state)
              │
              ▼
   edgehub → CloudCore → apiserver (Device.status.twins updated)
```

You now `kubectl get device sensor-warehouse-01 -o yaml` from your laptop and see the device's reported state — even though the device itself only speaks MQTT.

### 13.4 Why This Matters

Before KubeEdge, every IoT platform invented its own device model: AWS IoT shadows, Azure IoT twins, custom REST APIs. KubeEdge made it a Kubernetes CRD with desired/reported semantics. Now you can:

- Use `kubectl` and standard RBAC for device authorization.
- Write controllers that reconcile device state (e.g., a "set every fridge below 4°C to alarm" controller is just a watch on `Device.status`).
- Treat devices like Pods in the operator pattern (ch 23).

---

## 14. EdgeMesh: East-West Networking Across the Boundary

KubeEdge by default has no cross-node networking — each edge node is an island. **EdgeMesh** is the optional component that gives Pod-to-Pod connectivity across the edge↔cloud boundary.

### 14.1 Architecture

```
              ┌──────────────────┐
              │  Cloud Pod A     │
              │  10.244.1.5      │
              └──────────┬───────┘
                         │
                ┌────────▼────────┐
                │ EdgeMesh proxy  │  (daemonset)
                │  on each node   │
                └────────┬────────┘
                         │ libp2p (NAT-traversing P2P)
                         │
            ┌────────────┴───────────┐
            ▼                        ▼
    ┌──────────────┐         ┌──────────────┐
    │ EdgeMesh on  │         │ EdgeMesh on  │
    │ Edge Node 1  │         │ Edge Node 2  │
    └──────┬───────┘         └──────┬───────┘
           │                        │
    ┌──────▼───────┐         ┌──────▼───────┐
    │ Edge Pod B   │         │ Edge Pod C   │
    │ 10.244.5.10  │         │ 10.244.6.20  │
    └──────────────┘         └──────────────┘
```

Each node runs an EdgeMesh daemon that:

1. Watches Services and Endpoints from metamanager (or apiserver if on cloud).
2. Intercepts traffic to ClusterIPs using iptables (similar to kube-proxy iptables mode).
3. Routes the traffic to the appropriate node — across the WAN if necessary — using a libp2p mesh.
4. Uses NAT traversal (STUN, hole punching) so edge nodes behind firewalls can reach each other.

This makes Service DNS resolution work end-to-end. You can `curl http://my-service.edge-default.svc.cluster.local` from a cloud pod and have it hit an edge pod.

### 14.2 Latency Caveat

Cross-boundary traffic obviously inherits WAN latency. EdgeMesh is fine for control-plane RPCs, not for data-plane bursts. Most architectures use it sparingly: cloud orchestration calls into edge pods for occasional inference requests, but heavy edge traffic stays edge-local.

---

## 15. OpenYurt: The Least Invasive Edge Approach

Repository: `openyurtio/openyurt`. Originated at Alibaba; CNCF sandbox. Philosophy: do not replace any Kubernetes component, just add edge-friendly proxies and controllers on top.

### 15.1 Architecture

```
   ┌──────────────────────────────────────────────────────────┐
   │                Cloud K8s Cluster (vanilla)               │
   │                                                          │
   │   apiserver / etcd / controllers / scheduler             │
   │                                                          │
   │   +  edge-controller-manager (OpenYurt)                  │
   │   +  yurt-app-manager (NodePool, YurtAppSet)             │
   │   +  yurt-tunnel-server                                  │
   └──────────────────────────────┬───────────────────────────┘
                                  │ tunnel (HTTPS over reverse-proxy)
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
        ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
        │ Edge Node A │   │ Edge Node B │   │ Edge Node C │
        │             │   │             │   │             │
        │ yurthub     │   │ yurthub     │   │ yurthub     │
        │  (local     │   │  (local     │   │  (local     │
        │   apiserver │   │   apiserver │   │   apiserver │
        │   proxy)    │   │   proxy)    │   │   proxy)    │
        │     ▲       │   │     ▲       │   │     ▲       │
        │     │       │   │     │       │   │     │       │
        │  kubelet    │   │  kubelet    │   │  kubelet    │
        │  kube-proxy │   │  kube-proxy │   │  kube-proxy │
        │  yurt-tunnel│   │  yurt-tunnel│   │  yurt-tunnel│
        │  -agent     │   │  -agent     │   │  -agent     │
        └─────────────┘   └─────────────┘   └─────────────┘
```

### 15.2 yurthub: The Local API Proxy

`yurthub` runs on every edge node. The kubelet, kube-proxy, and any other component on the node is configured to talk to `https://127.0.0.1:10261` (yurthub) instead of the real apiserver. yurthub:

- **Proxies** requests upstream to the apiserver when the link is up.
- **Caches** responses — every Watch result is persisted to local disk under `/etc/kubernetes/cache/`.
- **Serves from cache** when the apiserver is unreachable. The kubelet thinks it is still talking to a working apiserver and continues to reconcile pods.

```
        kubelet           yurthub             apiserver (cloud)
          │                  │                       │
          │   GET /api/v1/   │                       │
          │   pods?watch    │                       │
          │ ───────────────► │                       │
          │                  │ proxy if connected   │
          │                  │ ────────────────────► │
          │                  │ ◄──────────────────── │
          │ ◄──────────────  │                       │
          │                  │ (also cached to disk) │
          │                  │                       │
          │ ----------- network drop ----------------│
          │                  │                       │
          │   GET /api/v1/   │                       │
          │   pods?watch    │                       │
          │ ───────────────► │                       │
          │                  │ serve from cache      │
          │ ◄──────────────  │                       │
          │                  │                       │
```

### 15.3 NodePool: Group Edge Sites

OpenYurt adds a `NodePool` CRD: a logical grouping of nodes (typically "one node pool per site"). NodePool gives you:

- A scoping primitive for placement (workload runs on this pool).
- A unit of operation for OTA (upgrade this pool first).
- An ownership/tenancy concept.

```yaml
apiVersion: apps.openyurt.io/v1beta1
kind: NodePool
metadata:
  name: store-042
spec:
  type: Edge
  selector:
    matchLabels:
      openyurt.io/desired-nodepool: store-042
  annotations:
    description: "Retail store 042, mall A, US-East"
```

### 15.4 YurtAppSet: Per-Pool Deployments

YurtAppSet is OpenYurt's analog of Deployment for fleet-wide apps. Like an ArgoCD ApplicationSet, it generates a per-pool Deployment from a single object.

```yaml
apiVersion: apps.openyurt.io/v1beta1
kind: YurtAppSet
metadata:
  name: pos-frontend
  namespace: retail
spec:
  workload:
    workloadTemplate:
      deploymentTemplate:
        metadata:
          labels:
            app: pos-frontend
        spec:
          replicas: 2
          selector:
            matchLabels:
              app: pos-frontend
          template:
            metadata:
              labels:
                app: pos-frontend
            spec:
              containers:
                - name: pos
                  image: registry.example.com/pos:v3.4.1
  topology:
    pools:
      - name: store-001
        nodeSelectorTerm:
          matchExpressions:
            - key: openyurt.io/nodepool
              operator: In
              values: ["store-001"]
        replicas: 2
      - name: store-042
        nodeSelectorTerm:
          matchExpressions:
            - key: openyurt.io/nodepool
              operator: In
              values: ["store-042"]
        replicas: 2
```

The yurt-app-manager controller materializes one Deployment per pool. Roll out per-pool, observe per-pool. Compare with KubeEdge — where pods are individually targeted via node selector — and you see why YurtAppSet is cleaner for fleet-wide apps.

### 15.5 Yurt Tunnel

To allow `kubectl logs` / `kubectl exec` from the cloud apiserver to reach edge pods (which sit behind NAT and cannot accept inbound connections), OpenYurt deploys `yurt-tunnel-server` in the cloud and `yurt-tunnel-agent` on each edge. The agent dials out, holds a reverse tunnel. When the apiserver wants to reach the edge kubelet's port 10250, it goes through the tunnel.

This is the same trick KubeEdge does — a reverse tunnel. The edge never has to accept inbound connections, which is essential for nodes behind NAT and for zero-trust posture.

### 15.6 OpenYurt vs KubeEdge

| | OpenYurt | KubeEdge |
|---|----------|----------|
| Invasiveness | Adds proxies; original K8s untouched | Replaces kubelet with edged |
| Device modeling | None built-in | Device + DeviceModel CRDs |
| Offline cache | yurthub on each node | metamanager (SQLite) |
| Workload abstraction | YurtAppSet (per-pool) | Standard Pod with node selector |
| Best for | Existing K8s shops adding edge | Greenfield IoT-heavy deployments |

OpenYurt is the right choice when you already have a vanilla K8s and want to "edge-enable" a subset of nodes. KubeEdge is the right choice when devices are first-class objects you want to manage with K8s primitives.

---

## 16. Akri: Device Discovery and Brokers

Repository: `project-akri/akri`. Microsoft's contribution to the device-on-the-network problem. CNCF sandbox.

### 16.1 The Problem Akri Solves

K8s has the device-plugin API (ch 21) for "devices attached locally to a node" — GPUs, FPGAs, etc. But what about devices on the network? A camera at IP 192.168.1.50 speaking ONVIF. A PLC at 10.0.5.20 speaking OPC UA. A USB temperature sensor that shows up under `/dev/ttyUSB0` on one node and `/dev/ttyUSB1` on another.

Akri's answer: **discover devices via protocol-specific handlers, model each discovered device as an Akri Instance, schedule a "broker" pod per Instance.**

### 16.2 Architecture

```
   ┌──────────────────────────────────────────────────────┐
   │                  Akri Controller                      │
   │  - Watches Configuration CRs                          │
   │  - Watches Instance CRs                                │
   │  - Reconciles broker Pods/Jobs                        │
   └────────────────────────┬─────────────────────────────┘
                            │ apiserver
                            ▼
   ┌──────────────────────────────────────────────────────┐
   │                kube-apiserver / etcd                  │
   │  Configurations, Instances                            │
   └────────────────────────┬─────────────────────────────┘
                            │
                ┌───────────┼───────────┐
                ▼           ▼           ▼
        ┌──────────────────────────────────┐
        │           Node A                  │
        │                                   │
        │  ┌─────────────────────────────┐ │
        │  │       Akri Agent             │ │
        │  │   (daemonset, one per node)  │ │
        │  │                              │ │
        │  │   - reads Configurations     │ │
        │  │   - runs discovery handlers  │ │
        │  │   - reports as device-plugin │ │
        │  └────────┬─────────────────────┘ │
        │           │                       │
        │  ┌────────▼─────────┐             │
        │  │ Discovery Handler │  (ONVIF,   │
        │  │  (separate Pod)   │   OPC UA,  │
        │  └────────┬─────────┘   udev, ...) │
        │           │                       │
        │           ▼                       │
        │     LAN scan / probe               │
        │     for cameras, PLCs              │
        │           │                       │
        │           ▼                       │
        │   Reports devices → Instances     │
        │   created/updated                 │
        │                                   │
        │   ┌─────────────────┐             │
        │   │ Broker Pod      │  scheduled  │
        │   │ (per Instance)  │  to consume │
        │   └─────────────────┘             │
        └──────────────────────────────────┘
```

### 16.3 Akri Configuration: The Declaration

A `Configuration` declares "find all devices matching X protocol; for each, schedule pod Y".

```yaml
apiVersion: akri.sh/v0
kind: Configuration
metadata:
  name: onvif-cameras
  namespace: akri
spec:
  discoveryHandler:
    name: onvif
    discoveryDetails: |
      ipAddresses:
        action: Include
        items:
          - 192.168.1.0/24
      macAddresses:
        action: Include
        items: []
      scopes:
        action: Include
        items: []
      discoveryTimeoutSeconds: 5
  capacity: 1                          # one broker per device
  brokerSpec:
    brokerPodSpec:
      containers:
        - name: onvif-broker
          image: ghcr.io/project-akri/akri/onvif-video-broker:latest
          imagePullPolicy: IfNotPresent
          resources:
            requests:
              memory: "64Mi"
              cpu: "100m"
            limits:
              memory: "128Mi"
              cpu: "200m"
              "{{PLACEHOLDER}}": "1"   # the device resource
  instanceServiceSpec:
    type: ClusterIP
    ports:
      - name: grpc
        port: 8083
        targetPort: 8083
  configurationServiceSpec:
    type: ClusterIP
    ports:
      - name: grpc
        port: 8083
        targetPort: 8083
```

`{{PLACEHOLDER}}` is substituted with the device's resource name at scheduling — exactly the same flow as a GPU resource request, but now for a discovered network device.

### 16.4 Discovery Handlers

| Protocol | Use case | Discovery method |
|----------|----------|------------------|
| **ONVIF** | IP cameras | WS-Discovery multicast |
| **OPC UA** | Industrial / SCADA | LDS server, browse address space |
| **udev** | USB devices | udev rules on host |
| **debugEcho** | Testing | Returns a synthetic device list |
| Custom | Anything | Implement the gRPC `DiscoveryHandler` API |

The handler runs as a separate Pod (so Akri agent itself stays small), exposing the `DiscoveryHandler` gRPC interface that the agent calls.

### 16.5 Akri Instance: A Discovered Device

When a device is discovered, the agent creates an `Instance`:

```yaml
apiVersion: akri.sh/v0
kind: Instance
metadata:
  name: onvif-cameras-c7d8e9
  namespace: akri
spec:
  configurationName: onvif-cameras
  brokerProperties:
    ONVIF_DEVICE_SERVICE_URL: "http://192.168.1.50/onvif/device_service"
    ONVIF_DEVICE_IP: "192.168.1.50"
    ONVIF_DEVICE_UUID: "uuid:c7d8e9..."
  shared: false
  nodes:
    - edge-cam-gw-01
  deviceUsage:
    edge-cam-gw-01: ""
```

The Akri controller then schedules a Pod (the broker) per Instance, with the device's properties injected as environment variables. The broker is your code: it can stream RTSP video, expose a gRPC frame service, write to S3, run inference — whatever a "consumer of one camera" looks like for your application.

### 16.6 Capacity and Sharing

- `capacity: 1` — exclusive use; only one broker per device.
- `capacity: N` — N brokers may share the device (the agent rejects more once N is reached).
- `shared: true` — the device is shared across nodes (e.g., a camera reachable from multiple GW nodes). Akri picks one node to host the broker.

### 16.7 Akri vs Standard Device Plugins

| | Standard device plugin (ch 21) | Akri |
|---|--------------------------------|------|
| Locality | Device attached to the node | Device on the network or USB |
| Discovery | Static; plugin scans local hw | Dynamic; protocol probes |
| Broker | Just a resource request | A full broker Pod per device |
| Model | Resource (`nvidia.com/gpu: 1`) | CRD (`Configuration` + `Instance`) |

Akri is what you reach for to give K8s "eyes" into industrial protocols.

---

## 17. Edge Networking Concerns

The edge breaks every assumption network code makes. Let us enumerate.

### 17.1 Intermittent Connectivity

Plan for the link to be down 5–10% of the time. Cluster components must:

- Cache state locally (KubeEdge metamanager, OpenYurt yurthub).
- Use exponential backoff (capped) for reconnect — not constant retries.
- Buffer outgoing data with a size cap to avoid runaway memory.
- Distinguish "transient drop" from "permanent failure" (a Pod evicted vs the whole site being gone).

### 17.2 Bandwidth

Edge uplinks are 1–100 Mbps. Critical decisions:

- **Telemetry sampling**: don't ship every Prometheus scrape to the cloud. Aggregate locally (recording rules) and push minutely summaries.
- **Log shipping**: locally rotate logs with size caps; push only error/warn levels to cloud. Use a sidecar with sampling (vector, fluent-bit) per node.
- **Image pulls**: pre-cache images locally; mirror registries at regional points; never have 100 stores simultaneously pull a 500 MB image from Docker Hub.

### 17.3 Latency

For control loops (factory PLC writes, retail tap-to-pay, autonomous driving), end-to-end latency must be sub-50 ms. That kills any pattern where the edge has to round-trip to the cloud to decide. This is the main reason embedded edge clusters dominate latency-critical use cases — the decision happens at the local apiserver.

### 17.4 Multi-NIC

Edge nodes often have two networks: a "cluster" network (between nodes within the site) and a "device" network (to the cameras, sensors, PLCs). Reasons:

- Devices are on an industrial subnet you don't control.
- Security: keep IT and OT networks separate (Purdue model).
- Performance: dedicate one NIC to high-bandwidth video streams.

Solutions:

- **Multus** CNI plugin: attach multiple network interfaces to a Pod.
- **Calico/Cilium** with multiple pod subnets.
- **SR-IOV** for line-rate device access from a Pod.

---

## 18. Edge Security

The edge is hostile. Anyone with physical access can pull the SD card. The threat model is **physical**, not just network.

### 18.1 Physical Access

- **Tamper-resistant chassis**: locking enclosures, sealed bezels, intrusion detection switches.
- **Encrypted disk**: LUKS or BitLocker; key stored in TPM, sealed to PCRs. If someone yanks the disk and puts it in another machine, the key is unavailable.
- **TPM-backed boot**: Secure Boot with measured-boot attestation. The system refuses to boot if the boot chain has been tampered with.
- **Sealed firmware**: BIOS/UEFI password, no USB boot, fuse bits set on SoC vendors that allow it.

### 18.2 Network

- **Zero-trust**: every connection is mTLS-authenticated. The edge dials out; never accepts inbound.
- **No public ingress**: edge nodes have no public IPs. Cloud reaches them via reverse tunnels (KubeEdge tunnel, yurt-tunnel-agent, Tailscale, WireGuard).
- **VPN to cloud**: WireGuard or IPsec for the management plane.
- **Egress allowlists**: edge nodes can only talk to the registry, the cloud apiserver, the tunnel server, the time servers, and the syslog endpoint. Everything else is denied.

### 18.3 Updates

- **Signed images**: container images are signed (cosign / Sigstore, ch 27). The kubelet rejects unsigned ones via a policy (Sigstore Policy Controller).
- **Attestation**: at boot, the device attests to the cloud — TPM quote, measurement of boot chain, version of OS. If attestation fails, the device is quarantined.
- **Atomic OS updates**: A/B partitions (Talos, Ubuntu Core, Flatcar). If the new image fails to boot, automatic fallback.

### 18.4 Secrets

The edge stores some secrets locally — the join token, the API server cert, the WireGuard private key. These must be:

- Sealed to the TPM (so cloning the disk does not yield usable secrets).
- Rotated automatically (don't ship a static 5-year credential).
- Scoped narrowly — an edge node's credential should only allow it to register itself, not to read other sites' data.

---

## 19. OTA Updates at the Edge

OTA (over-the-air) is the make-or-break operational concern. A fleet that can't be safely updated is a fleet that grows insecure over time.

### 19.1 System Updates

| Distro | Update mechanism |
|--------|------------------|
| **Talos** | `talosctl upgrade --image ghcr.io/siderolabs/installer:vX.Y.Z`. A/B partitions. |
| **Ubuntu Core / MicroK8s** | `snap refresh`. Auto-refresh on a schedule; transactional. |
| **K3s on Ubuntu/Debian** | `system-upgrade-controller` CRDs; declarative node upgrades. |
| **Flatcar** | `update_engine`; A/B partitions; locksmith for ordered reboots. |
| **Bottlerocket** | Brupop operator; A/B partitions; reboots one node at a time. |

### 19.2 K3s System Upgrade Controller

K3s ships `system-upgrade-controller` which watches `Plan` CRDs and applies them to selected nodes:

```yaml
apiVersion: upgrade.cattle.io/v1
kind: Plan
metadata:
  name: k3s-server-upgrade
  namespace: system-upgrade
spec:
  concurrency: 1                       # only 1 server at a time
  cordon: true                          # cordon before upgrade
  nodeSelector:
    matchExpressions:
      - key: node-role.kubernetes.io/control-plane
        operator: Exists
  serviceAccountName: system-upgrade
  upgrade:
    image: rancher/k3s-upgrade
  version: v1.30.3+k3s1
```

The controller schedules a Pod per matching node that uses host mounts to swap the `k3s` binary and restart the service. This is **in-cluster, declarative, GitOps-driven** — exactly the pattern you want for 1000 sites.

### 19.3 App Updates: GitOps

The dominant edge app-update pattern is GitOps (ArgoCD or Flux). Two flavors:

**Pull from edge:**

```
   cloud Git repo  ──────── git pull ─────►  argocd in edge cluster
                                                     │
                                                     ▼
                                            apply manifests
```

Each edge cluster runs its own ArgoCD that pulls from a central Git. Resilient (works even when cloud apiserver is down, as long as Git is reachable), but more sites running ArgoCD.

**Push from cloud:**

```
   cloud Argo / Flux  ──────── kube push ─────►  edge apiserver
```

One central ArgoCD targets every edge apiserver. Simpler ops, but if the central goes down so do deploys, and ArgoCD needs to be able to reach each edge.

Most large fleets do hybrid: a Rancher Fleet-style "fleet controller" in the cloud bundles changes per cluster and pushes them, with retries.

### 19.4 ApplicationSet per Cluster

ArgoCD's `ApplicationSet` generates one `Application` per matched cluster — perfect for per-site rollout.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: retail-store-pos
  namespace: argocd
spec:
  generators:
    - clusters:
        selector:
          matchLabels:
            type: retail-edge
  template:
    metadata:
      name: '{{name}}-pos'
    spec:
      project: retail
      source:
        repoURL: https://git.example.com/retail-deploy
        targetRevision: HEAD
        path: 'overlays/{{name}}'
      destination:
        server: '{{server}}'
        namespace: pos
      syncPolicy:
        automated:
          prune: true
          selfHeal: true
```

### 19.5 Phased Rollouts

Never roll to all sites at once. The standard cadence:

1. **Canary**: 1% of sites, low-traffic.
2. **Bake**: 24h observation, automatic SLO check.
3. **Early adopter**: 10%.
4. **Bake**: 24h.
5. **Majority**: 50%.
6. **Bake**: 24h.
7. **Full**: 100%.

Rancher Fleet's "rollout strategy" and ArgoCD's progressive sync hooks both support this. Automated halt-on-error is non-negotiable.

---

## 20. Edge Storage

The edge has no SAN, no cloud block storage, no managed object store close by. You get what is on the box.

### 20.1 Local-Only Options

| Option | Pros | Cons |
|--------|------|------|
| **hostPath** | Trivial | No PVC binding, no quota, not portable |
| **local-path-provisioner** (K3s) | Dynamic PVC against host disk | No replication |
| **OpenEBS LocalPV** (hostpath / device) | Dynamic, simple | No replication |
| **OpenEBS Mayastor / Replicated PV** | Replicated across nodes | More overhead; needs HugePages and CPU |
| **TopoLVM** | LVM thin-provisioning, snapshots | Linux-specific |
| **Longhorn** | Replicated block storage; UI | Heavier; ARM support limited until v1.5+ |

### 20.2 Why No Remote PVC

You cannot mount an EBS volume in a wind turbine. The block-storage backend has to be local to the site. So all dynamic provisioning is local; multi-replica resilience is achieved by replicating across local nodes (Mayastor, Longhorn) or by app-level replication (the database does its own redundancy).

### 20.3 Stateful Workloads Pinned

A StatefulSet pod's PVC is local to a node. If the node dies, the pod cannot be rescheduled (the PVC isn't reachable). You either:

- Pin the StatefulSet to the same node permanently (and accept downtime on node failure until the node returns).
- Use a replicated storage layer (Longhorn / Mayastor) that survives one node failure.
- Use app-level HA (Postgres streaming replication, Redis Sentinel) and accept dataloss tolerance.

### 20.4 Backups

Local storage means backups must leave the site. Velero (with restic) writing to an S3-compatible cloud bucket is the standard. Schedule nightly during the off-peak hour. Encrypted at rest, signed.

```yaml
apiVersion: velero.io/v1
kind: Schedule
metadata:
  name: nightly-edge-backup
  namespace: velero
spec:
  schedule: "0 2 * * *"
  template:
    includedNamespaces:
      - pos
      - inventory
    storageLocation: cloud-backup
    ttl: 168h
    defaultVolumesToFsBackup: true
```

---

## 21. Edge Use Cases

### 21.1 Retail

A per-store K3s cluster (3 small nodes in the back-of-store). Workloads:

- **POS frontend** (DaemonSet per till, one Pod per terminal).
- **Inventory cache** (StatefulSet with local PV; syncs to cloud nightly).
- **Promotion engine** (a few replicas of a stateless API).
- **Camera analytics** (Akri for camera discovery + GPU-backed broker pods).
- **Local sync agent** (pushes transactions to cloud asynchronously).

Failure: an internet outage at the store **must not** stop sales. POS continues against the local inventory cache; transactions queue locally and flush on reconnect.

### 21.2 Industrial / Factory

Per-factory K3s + Akri for the PLCs and HMI tablets:

- **Akri Configuration** for OPC UA → broker pods that bridge to MQTT or Kafka.
- **Time-series ingest** locally (VictoriaMetrics, InfluxDB) before pushing summaries up.
- **Edge ML** (defect detection on cameras via NVIDIA Jetson nodes, GPU device plugin + Triton).
- **Air-gap** standard; updates via signed USB or one-direction-only proxy.

### 21.3 Automotive

A per-vehicle K3s on the in-vehicle compute (typically ARM64 SoC, 8–16 cores, 16 GB RAM):

- Strict offline tolerance: cellular drops in tunnels, parking garages, dead zones.
- Signed OTA via cloud → vehicle. Container images and OS updates both signed.
- Workloads: telematics, infotainment, ADAS subsystems running as Pods with `Guaranteed` QoS and HugePages.
- Vehicle ↔ cloud sync via MQTT or AVTP, not direct kube/HTTPS.

### 21.4 Telco / 5G MEC

5G User Plane Function (UPF) and edge applications run on commodity x86 in **MEC sites** (sometimes a single rack per cell tower aggregation point):

- KubeEdge or vanilla K8s with EdgeMesh — depending on whether site is large or small.
- DPDK / SR-IOV for line-rate packet processing.
- Tens of milliseconds latency from UE to application.
- Tight integration with 5G core for QoS signaling.

### 21.5 Remote Sites (Oil/Gas/Maritime)

Long-latency, low-bandwidth links (satellite, microwave). OpenYurt is a strong fit:

- yurthub gives offline tolerance per node.
- NodePool models per-site groupings.
- Apps that mostly stay local; cloud is read-only dashboard.
- Updates pulled overnight when the bird is overhead.

---

## 22. Cluster-as-a-Service for Edge

A few platforms target the operations burden of "1000 clusters":

| Platform | Vendor | What it manages |
|----------|--------|-----------------|
| **Rancher Fleet** | SUSE | Fleet-wide GitOps for K3s/RKE2 |
| **AWS EKS Anywhere** | AWS | On-prem K8s, lifecycle-managed |
| **GKE on-prem / Distributed Cloud** | Google | Google-managed control plane shipped to your hardware |
| **Azure Arc-enabled K8s** | Microsoft | Adds Azure control plane on top of any K8s |
| **Tanzu Edge** | VMware/Broadcom | Lifecycle of vSphere-based edge clusters |
| **Sidero Omni** | Sidero Labs | SaaS for Talos fleet management |

The unifying idea: the **central platform handles** join, upgrade, observability, certificate rotation, GitOps wiring. You focus on apps. This is essential at fleet sizes ≥100 sites.

---

## 23. Edge-Specific Kubelet Tunings

The default kubelet config is tuned for a datacenter. At the edge you want:

### 23.1 Aggressive Image GC

```yaml
imageGCHighThresholdPercent: 70    # GC when disk usage hits 70%
imageGCLowThresholdPercent: 50     # Stop GC when down to 50%
imageMinimumGCAge: 1m              # Eligible after 1 minute idle
```

Default thresholds (85/80, age 2m) leave too little headroom on a 32 GB eMMC.

### 23.2 Reduced Metric Scrape Frequency

In `kubelet` config:

```yaml
serializeImagePulls: false
maxParallelImagePulls: 2           # but limit to 2 for slow links
streamingConnectionIdleTimeout: 10m # less aggressive than default 4h
```

In Prometheus / vmagent:

```yaml
scrape_interval: 60s               # vs 15s default
# Drop high-cardinality metrics at the scrape:
metric_relabel_configs:
  - source_labels: [__name__]
    regex: 'apiserver_request_.*'
    action: drop
```

### 23.3 Local Logging Only with Size-Limited Rotation

```yaml
# /etc/docker/daemon.json or containerd config
"log-driver": "json-file",
"log-opts": {
  "max-size": "10m",
  "max-file": "3"
}
```

Anything more than 30 MB per container log on a 32 GB disk is a disk-full waiting to happen.

### 23.4 Hard Limits on System Pods

Make sure CoreDNS, kube-proxy, metrics-server have **resource limits** — not just requests — so they cannot eat your workload's memory under pressure.

```yaml
resources:
  requests:
    cpu: 50m
    memory: 50Mi
  limits:
    cpu: 200m
    memory: 150Mi
```

### 23.5 Eviction Thresholds

```yaml
evictionHard:
  memory.available: "100Mi"
  nodefs.available: "5%"
  nodefs.inodesFree: "5%"
  imagefs.available: "10%"
evictionSoft:
  memory.available: "200Mi"
evictionSoftGracePeriod:
  memory.available: "30s"
```

At the edge you want eviction to kick in *earlier* than default so the kernel OOM killer never has to choose for you.

---

## 24. The Single-Node Cluster

Yes, "single-node K3s" is a legitimate production topology — for certain use cases.

### 24.1 When It Is OK

- **IoT gateway**: a small box that talks to local devices and forwards data. Single point of failure is acceptable because the *devices* (sensors, etc.) are the redundancy. If the gateway dies, you ship a new one.
- **Home / SOHO use**: home automation, NAS, dev cluster.
- **Lab / demo**: anything throwaway.

### 24.2 When It Is Not OK

- Anything where workload downtime > 5 minutes is a business problem.
- Anything with stateful data that cannot be lost (the disk dies, the data dies).
- Anything that needs control-plane redundancy.

### 24.3 Best Practices for Single-Node

- **Take backups off the device**: sqlite snapshot + Velero PVC backups + image of the system.
- **Watchdog**: a hardware watchdog (most SoCs have one) that hard-reboots if the kernel hangs.
- **Auto-recover boot**: cloud-init or first-boot scripts that re-bootstrap if the data partition is wiped.
- **Remote KVM** if possible (IPMI, BMC) for last-resort access.

---

## 25. HA at the Edge

3-node K3s with embedded etcd is the standard production HA setup. Some subtleties.

### 25.1 Cross-Failure-Domain Placement

```
         BAD: 3 servers in one chassis
   ┌──────────────────────────────┐
   │  One physical box (PDU A)     │
   │  - srv1                       │
   │  - srv2                       │
   │  - srv3                       │
   └──────────────────────────────┘
        ↓ power outage → all gone

         GOOD: 3 servers, 3 failure domains
   ┌────────────┐  ┌────────────┐  ┌────────────┐
   │ Chassis 1  │  │ Chassis 2  │  │ Chassis 3  │
   │ PDU A      │  │ PDU B      │  │ PDU C      │
   │ Switch X   │  │ Switch X   │  │ Switch Y   │
   │ srv1       │  │ srv2       │  │ srv3       │
   └────────────┘  └────────────┘  └────────────┘
```

At the edge "failure domain" might be:

- Different physical chassis.
- Different shelves in the same rack (PDU independence).
- Different switches.
- Different UPS feeds.

You will not get true geo-redundancy at the site (etcd cannot stretch). What you do get is: **at least one component-level failure does not take the site down**.

### 25.2 Quorum Math

Embedded etcd requires (N/2)+1 alive to form quorum. For 3 nodes, that is 2. Lose 1, fine; lose 2, the cluster is read-only and refuses writes until quorum returns.

Five nodes give you tolerance for 2 failures at the cost of double the write latency (commits must reach 3 of 5). At the edge, 3 is usually right; 5 is rare.

### 25.3 Read-Only Mode

When quorum is lost, the apiserver still serves reads from the leader's last view but rejects writes. Edge workloads (pods already running) continue to operate; you just can't deploy new ones until quorum returns. This is the right default — better than a split brain.

---

## 26. Cloud-Burst from the Edge

A more theoretical pattern: when the edge runs out of capacity, burst workloads to a cloud cluster.

### 26.1 The Architecture

```
    ┌──────────────┐  Karmada / Submariner   ┌──────────────┐
    │ Edge cluster │ ◄──────────────────────► │ Cloud cluster │
    │ (constrained)│   pod federation,         │ (elastic)     │
    │              │   service discovery       │              │
    └──────────────┘                          └──────────────┘
        ▲                                         ▲
        │ normal workload                         │ burst workload
        │                                         │
        └────────── application traffic ──────────┘
```

When local capacity is exhausted, the federation controller (Karmada, KubeFed, Liqo) schedules excess Pods onto the cloud cluster. Services are stretched across clusters via Submariner (multi-cluster routing) or Cilium ClusterMesh.

### 26.2 Why It Is Rare in Practice

- **Latency penalty**: cloud-resident pods serving edge users is several round-trips slower than edge-local.
- **Bandwidth cost**: shipping the request traffic back and forth burns the uplink.
- **Stateful workloads**: cannot move state to the cloud and back without coordination.
- **Operational complexity**: federation is one of the harder topologies to debug.

Cloud-burst exists. It is right for niche cases (e.g., transient analytics jobs at the edge that can offload). For most edge workloads, you size for peak locally.

---

## 27. Operating an Edge Fleet at Scale

Let us assume 500 sites. What do you need?

### 27.1 Cluster Bootstrap Automation

The device arrives unprovisioned. You need it to come up usable with zero on-site keystrokes.

```
   ┌──────────────────────────────────────────────────────┐
   │   Device powered on                                   │
   │     ↓                                                 │
   │   PXE / iPXE boot from in-store DHCP server          │
   │     ↓                                                 │
   │   Downloads ISO / kernel from staging server          │
   │     ↓                                                 │
   │   cloud-init / Talos machine config injected         │
   │     ↓                                                 │
   │   Reads config, sets hostname, joins cluster          │
   │     ↓                                                 │
   │   Registers in inventory; status visible in cloud    │
   └──────────────────────────────────────────────────────┘
```

For Talos: ship a Talos image with embedded "discover" config, machine config served via metadata HTTP. For Ubuntu: cloud-init user-data. For K3s on top: a `k3s install` line in cloud-init.

### 27.2 Inventory

A central inventory database (Postgres, or a CRD in the cloud cluster) holds:

- Site ID, location, hardware model, MAC addresses.
- OS version, K8s version, last-seen.
- Workload versions deployed.
- Open issues.

Critical for "site #42 is offline" → "is it a hardware issue (RMA), a network issue (ISP ticket), or a software issue (reboot)?"

### 27.3 GitOps for App Config

One Git repo per app, parameterized per site via Kustomize overlays or Helm value files. ArgoCD or Fleet renders and pushes.

### 27.4 Centralized Observability

```
   Each edge node:
     ├── vmagent / fluent-bit (local buffer)
     │      │
     │      └── push when network up
     │            │
     │            ▼
     ▼      Cloud Cortex / Loki / Mimir
   Local disk buffer
   (size-capped ring)
```

Local buffering with a size cap — when the link is down, the buffer fills; when it comes back, drains. Lose only the data that overflowed the buffer, not all of it.

### 27.5 Swap Procedure

Hardware fails. The replacement workflow:

1. Detect: alert "node X offline for >30 minutes" + "no reboot success".
2. Ship: an identical pre-imaged unit (or have it on-site as cold spare).
3. Swap: an on-site tech (store manager) physically swaps. No keystrokes; the new unit auto-bootstraps from cloud-init.
4. Reconcile: cluster sees the new node, ArgoCD redeploys.
5. Retire: cordon, delete, decommission the old.

This is much easier with immutable distros (Talos, Flatcar, Ubuntu Core) — every node is bit-identical.

---

## 28. What Edge K8s Gives Up

You lose the cloud's managed services. Each must be replaced.

### 28.1 Cloud Load Balancers

Replace with:

- **Klipper-LB** (K3s default): iptables-on-every-node; simple.
- **MetalLB**: L2 (ARP-announce VIPs) or BGP (announce to your router).
- **kube-vip**: leader-elected VIP for control plane and services.

```yaml
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: store-pool
  namespace: metallb-system
spec:
  addresses:
    - 192.168.10.240-192.168.10.250
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: store-l2
  namespace: metallb-system
spec:
  ipAddressPools:
    - store-pool
```

### 28.2 Cloud CSI Drivers

Replace with:

- **local-path-provisioner** (K3s default).
- **TopoLVM** for LVM thin-provisioning with snapshots.
- **OpenEBS Mayastor / Longhorn** for replicated block storage.
- **NFS / SMB CSI** if you have a local NAS.

### 28.3 Cloud DNS for Ingress

Replace with:

- **ExternalDNS** plus your cloud DNS provider (you keep DNS in the cloud, just push from edge).
- **Static IPs** with a wildcard DNS pointing at the site's public IP.
- **Service mesh ingress** (Istio, Linkerd) with mTLS-only access from the cloud.

### 28.4 Cloud IAM

Replace with:

- **OIDC** against a central IdP (Keycloak, Okta, Google Workspace).
- **Cert-manager** to mint short-lived workload identities.
- **SPIFFE/SPIRE** for cross-cluster workload identity.

---

## 29. Bare-Metal Kubernetes

Adjacent to edge but distinct: "I run K8s in my own racks, not in any cloud." Many of the edge tools apply.

### 29.1 The Bare-Metal Stack

```
┌──────────────────────────────────────────────────────────┐
│  Workloads                                                │
├──────────────────────────────────────────────────────────┤
│  Service mesh (Istio / Linkerd / Cilium ServiceMesh)     │
├──────────────────────────────────────────────────────────┤
│  Ingress (Envoy Gateway / Nginx / Traefik)               │
├──────────────────────────────────────────────────────────┤
│  Service LB (MetalLB / kube-vip / Cilium L2 / BGP)       │
├──────────────────────────────────────────────────────────┤
│  CNI (Cilium / Calico with BGP)                          │
├──────────────────────────────────────────────────────────┤
│  Storage (Rook+Ceph / Longhorn / Mayastor)               │
├──────────────────────────────────────────────────────────┤
│  Kubernetes (kubeadm / k0s / Talos / RKE2)               │
├──────────────────────────────────────────────────────────┤
│  Container runtime (containerd / CRI-O)                  │
├──────────────────────────────────────────────────────────┤
│  Host OS (Talos / Ubuntu / Flatcar / RHEL CoreOS)        │
├──────────────────────────────────────────────────────────┤
│  Hardware                                                  │
└──────────────────────────────────────────────────────────┘
```

### 29.2 BGP-Announced Service IPs

The bare-metal alternative to cloud LBs: speak BGP to your top-of-rack switches. MetalLB or Cilium can do this. Each Service of type LoadBalancer gets a VIP that is announced to the network. Anycast: the same VIP is announced from every node, the router ECMPs traffic.

```yaml
apiVersion: cilium.io/v2alpha1
kind: CiliumBGPClusterConfig
metadata:
  name: cilium-bgp
spec:
  nodeSelector:
    matchLabels:
      bgp: enabled
  bgpInstances:
    - name: "instance-65000"
      localASN: 65000
      peers:
        - name: "tor-1"
          peerASN: 65001
          peerAddress: 10.0.0.1
          peerConfigRef:
            name: cilium-peer-config
```

### 29.3 Rook + Ceph

If you have ≥5 disks per node and ≥3 nodes, Rook deploys a self-healing distributed Ceph cluster. Pros: replication, snapshots, S3-compatible RGW, RBD for block, CephFS for shared. Cons: heavy (Ceph wants 1 GB RAM per OSD plus CPU); operational complexity. Suitable for "datacenter-class bare metal", not for a 3-node retail site.

---

## 30. Migration Patterns

### 30.1 Embedded → K3s

Many retail / industrial systems are legacy embedded Linux running a couple of binaries under systemd. Migration:

1. Containerize the binaries (often a 2-day job per app).
2. Replace systemd with K3s; convert services to Deployments / DaemonSets.
3. Move config from `/etc/<app>/conf.yaml` to ConfigMaps.
4. Move state from `/var/lib/<app>/` to PersistentVolumeClaims.
5. Wire up GitOps for deploys.

Outcome: the same hardware now ships standard K8s manifests, can be remotely updated, and inherits the K8s ecosystem for monitoring, secrets, networking.

### 30.2 Per-Store VMs → Per-Store K3s

Retail and bank branches often had a VM per app at each site. Migration:

1. Inventory the per-site VMs (compute, memory, disk).
2. Pick K3s sizes that exceed the largest VM by 1.5x.
3. Containerize VM workloads (or use KubeVirt to run the VM-as-pod during transition).
4. Replicate state to a K8s PVC.
5. Cutover one app at a time per site.

KubeVirt is the bridge: it lets you run unmodified VMs inside K8s while you migrate piece by piece.

### 30.3 Standalone Docker → K3s

The simplest migration. Many edge appliances run a few `docker compose` services. Migration:

1. `kompose convert` from docker-compose to K8s YAML.
2. Install K3s on the device.
3. `kubectl apply` the converted manifests.
4. Configure local-path-provisioner for persistent volumes.

You gain: declarative state, restart policies, health checks, service discovery, GitOps. You lose: nothing meaningful.

---

## 31. Pitfalls

The edge has its own catalogue of failure modes. None are obscure once you have seen them, all are painful the first time.

1. **K3s sqlite on single replica**. Disk failure → entire cluster state lost. Restore from snapshot only. Solution: 3-node embedded etcd, or accept the data loss risk for stateless workloads.
2. **K3s embedded etcd on slow SD card**. SD-card sync latencies of 50–200 ms become apiserver latencies. Symptom: slow `kubectl`, slow scheduler. Solution: eMMC or SSD, never SD.
3. **MicroK8s snap auto-refresh in production**. Snap silently upgrades K8s minor version, breaks something. Solution: `snap refresh --hold microk8s` and pin to a specific channel.
4. **Talos forgot machine config**. You lose the config bundle (kubeconfig + talosconfig). The cluster is fine but you cannot manage it. Solution: store these in a vault, back them up with the rest of cluster secrets, treat as gold.
5. **KubeEdge CloudCore exposed to internet without auth**. The CloudCore tunnel listener is public; anyone with a bootstrap token can register. Solution: rotate bootstrap tokens (short TTL), require mTLS at the LB, audit who registered.
6. **OpenYurt without yurthub on a node**. The node is in the cluster but has no offline tolerance — when the link drops, kubelet starts evicting. Solution: ensure every edge node has yurthub installed and bypasses go through it.
7. **Akri broker pod evicted under memory pressure**. The device becomes "unavailable" until the broker comes back. Solution: set Guaranteed QoS on brokers, eviction protection annotations.
8. **Flannel VXLAN on slow-CPU edge devices**. Encapsulation overhead is 5–10% of CPU on a Cortex-A53. Solution: host-gw on flat L2, or wireguard-native (faster on hardware that has crypto accel).
9. **Single-node "cluster" without backups**. Disk dies; data dies. Solution: nightly sqlite snapshots + Velero PVC backups to cloud.
10. **Edge fleet without GitOps**. Sites drift, snowflake configs accumulate. Solution: Fleet/ArgoCD from day one; no kubectl-apply from a laptop ever.
11. **Over-aggressive log retention**. /var/log fills the disk, kubelet starts evicting, cluster goes red. Solution: container log size caps + filebeat shipping with local buffer caps.
12. **Cluster bootstrap requires internet that's flaky**. Half the fleet fails to provision because the registry pull retries timeout. Solution: offline mirror at the staging site / regional CDN.
13. **Shipping non-airgapped images to an airgap site**. Image references docker.io/...; the pull fails on-site. Solution: airgap image bundle as part of the install pipeline; rewrite image references to a local mirror.
14. **Per-site cluster cert expiry overlapping**. All 100 sites bootstrapped on the same day → all certificates expire on the same day → mass outage one year later. Solution: stagger initial provisioning or use a CA with a `auto-rotate` controller.
15. **kubelet `--serialize-image-pulls` on a slow link**. Pods queue behind a slow image pull. Solution: `--serialize-image-pulls=false --max-parallel-image-pulls=2`. Two is enough not to saturate.
16. **EdgeCore WebSocket dropped without reconnect handling**. Pod not updated; configmap changes never reach. Solution: configure aggressive ping/keepalive; alert on long disconnections.
17. **EOL distribution**. You picked an edge distro that the vendor stopped supporting. No security patches. Solution: pick a distro with a clear LTS commitment (K3s, RKE2, Talos, Ubuntu Core all qualify); track upstream EOLs.
18. **Device-level secret in plaintext**. The bootstrap token sits in `/etc/<app>/secrets.yaml` world-readable. Solution: sealed-to-TPM, restricted file permissions, secret rotated post-bootstrap.
19. **Operating system A/B partitions without rollback testing**. The new image boots once; you never test fallback. The day a bad image ships, the fallback also fails. Solution: chaos-test the fallback path in CI.
20. **CoreDNS without limits**. CoreDNS on the edge can be hammered by a runaway pod and OOM the host. Solution: hard memory limits on every system pod.
21. **MetalLB L2 mode with too small a pool**. Service-LB allocation fails when more than N services exist. Solution: size pools 2x current need.
22. **Image pull without local cache**. Every node pulls the same 500 MB image from the cloud, saturating the 50 Mbps uplink. Solution: local registry mirror per site; or distroless images small enough to not matter.
23. **Velero backups but no restore drill**. Backups silently fail or are incomplete; only discovered at recovery time. Solution: quarterly restore-from-cloud drill on a spare device.
24. **Trying to stretch etcd across sites**. WAN latency murders Raft commits, the cluster goes split-brain or never elects a leader. Solution: separate clusters per site, multi-cluster federation if you need cross-site visibility.

---

## 32. Closing Thoughts

The edge is where Kubernetes becomes uncomfortable: the things it assumes (8 GiB of RAM, a 1 Gbps link to the apiserver, a human at the keyboard) are exactly the things the edge cannot guarantee. The distributions in this chapter — K3s, MicroK8s, k0s, Talos, KubeEdge, OpenYurt, Akri — are different responses to that mismatch, and each makes sense for a different axis of "what the edge gives up".

A few staff-level rules of thumb to leave you with:

- **Pick the architectural pattern first**. Embedded clusters at every site vs centralized control plane is the call that decides everything else. Get this wrong and you spend years pulling against the grain.
- **Treat the fleet as the unit of operations**. One cluster is easy; 500 clusters is operations engineering. GitOps, immutable images, signed updates, per-site identity, declarative inventory — without these, scale eats you.
- **Pay for offline tolerance up-front**. Either the workload runs without the cloud (embedded cluster, OpenYurt yurthub, KubeEdge metamanager) or it doesn't. There is no "we'll add it later" — local caching has to be designed in from the start.
- **Imagine the swap procedure**. If a node fails, a non-technical person needs to be able to replace it. Bake that into your stack: immutable OS, auto-bootstrap, cloud-init, no on-site state. If a tech needs to SSH in to fix anything, you have already lost the operations battle.
- **Edge is mostly bare-metal**. The patterns from §29 apply: MetalLB, local storage, BGP, replicated PV, signed images. Make peace with operating at this layer; the cloud's training wheels are not coming.

Edge Kubernetes is one of the rare cases where the project was so successful at "running anywhere" that its constraints became visible. The distributions in this chapter sand off those constraints — and in doing so, they show what is essential about Kubernetes (the declarative reconciliation loop, the Pod abstraction, the CRD model) versus what is contingent (the heavy control plane, the cloud-provider integrations, the 4 GB memory floor). The essential survives. The contingent gets stripped.

Next chapter: the operational and observational tooling that ties this all together — what to monitor, what to alarm on, and how to debug a 500-site fleet at 2 AM.
