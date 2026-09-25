# Kubernetes Internals — Labs

A learn-by-doing sheet for the 45 chapters of this track. The chapters are the theory; each section here makes you rebuild, break, and measure what the chapter describes, below the API surface. Object-level practice (Pods, Deployments, Services, config, scheduling constraints, controllers, operators) already lives in [`../k8s-learn/`](../k8s-learn/README.md). Where a chapter overlaps, the section header tells you which sheet to do **first**; the tasks here go one layer down.

Rules:
- **Closed book first.** Try each task before rereading the chapter. Reread only to explain what you saw.
- **Predict before you run.** Write the prediction down, then the measurement. The gap is what you learn.
- **Write results down.** Every task ends in a number, an output, or a file. No number, not done.
- **Spaced review.** Redo each chapter's checkpoint questions from memory on a schedule.

## How to use this sheet
- Order: 00 → 11 in sequence (the substrate and the control plane), then pick by need. 39–44 are independent and can be done any time. 38 last.
- Tick `- [ ]` boxes as you go. Core tasks first; do Stretch tasks when a chapter is your current focus.
- Keep `lab-notebook.md` next to your cluster configs. One entry per task: date, prediction, command, result, one sentence on the gap.
- Answer each checkpoint closed book **before** rereading. Redo it after 1 day, 1 week, and 1 month. Mark misses; a question you miss twice becomes a task you redo.
- kind clusters are disposable. `kind delete cluster --name lab` and recreate rather than debug a half-broken lab. Tasks marked **destructive** assume that.

## Setup

| Environment | Cost | Used by |
|---|---|---|
| Laptop + Docker (or Podman), `kubectl`, `helm`, `jq`, `kind` ≥ 0.24 | Free | Almost everything |
| kind cluster `lab`: 1 control plane + 2 workers, audit log on (config below) | Free | 03–14, 18–25, 27–28, 30–32, 35–37, 44 |
| Extra kind clusters: `ipvs`, `nft`, `cilium`, `calico` (one-liners below) | Free | 14, 15, 16, 20 |
| Linux VM (Lima, Multipass, UTM, or a spare box) with root, cgroup v2, `util-linux`, `iproute2`, `runc`, `podman` | Free | 00, 01, 16 (bpftrace), 38 |
| `k3d` (k3s in Docker) | Free | 33, 25 (vCluster alt) |
| `minikube --container-runtime=containerd` with the `gvisor` addon, or `runsc` installed in the Linux VM | Free | 29 |
| `kwok` / `kwokctl` (fake nodes, real control plane) | Free | 09, 34, 35 |
| Local `registry:2` on `localhost:5000`; `ttl.sh` for throwaway public pushes (no account, images expire) | Free | 02, 27, 39, 43 |
| Go 1.22+, Python 3.12+ with `uv` | Free | 06, 08, 23, 24, 34, 38, 43 |
| Tools: `crane`, `cosign`, `syft`, `trivy`, `cilium` + `hubble` CLIs, `istioctl`, `hyperfine`, `dive`, `fio` | Free | Named per task |
| Any cloud account | Paid, **optional** | Only tasks marked "optional, cloud" |

**The shared `lab` cluster.** Create `lab/extra/` next to this config (use the absolute path in `hostPath`). Everything the apiserver must read later (audit policy, encryption config) goes in that directory, so enabling a feature is a one-line edit of the static Pod manifest.

```yaml
# lab/kind-lab.yaml  ->  kind create cluster --name lab --config lab/kind-lab.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraMounts:
  - {hostPath: /ABSOLUTE/PATH/lab/extra, containerPath: /etc/kubernetes/extra}
  kubeadmConfigPatches:
  - |
    kind: ClusterConfiguration
    apiServer:
      extraArgs:            # kubeadm v1beta4 wants a list: [{name: audit-log-path, value: ...}]
        audit-policy-file: /etc/kubernetes/extra/audit-policy.yaml
        audit-log-path: /var/log/kubernetes/audit.log
      extraVolumes:
      - {name: extra, hostPath: /etc/kubernetes/extra, mountPath: /etc/kubernetes/extra, readOnly: true, pathType: Directory}
      - {name: auditlog, hostPath: /var/log/kubernetes, mountPath: /var/log/kubernetes, pathType: DirectoryOrCreate}
- role: worker
- role: worker
```

```yaml
# lab/extra/audit-policy.yaml  (first matching rule wins)
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: ["RequestReceived"]
rules:
- level: None
  resources: [{group: "coordination.k8s.io", resources: ["leases"]}]
- level: None
  verbs: ["watch"]
- level: Metadata
  resources: [{group: "", resources: ["secrets", "configmaps"]}]
- level: Request
  resources: [{group: ""}, {group: "apps"}, {group: "batch"}, {group: "admissionregistration.k8s.io"}]
- level: Metadata
```

Helpers used throughout (put them in `lab/env.sh` and `source` it):

```bash
# etcdctl / etcdutl inside the kind etcd static Pod
ketcd() { kubectl -n kube-system exec etcd-lab-control-plane -- etcdctl \
  --endpoints=https://127.0.0.1:2379 --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt --key=/etc/kubernetes/pki/etcd/server.key "$@"; }
auditlog() { docker exec lab-control-plane cat /var/log/kubernetes/audit.log; }
# control-plane /metrics: 10259 scheduler, 10257 controller-manager, 2381 etcd (plain http)
kubectl -n kube-system create sa metrics-reader 2>/dev/null
kubectl create clusterrolebinding metrics-reader --clusterrole=system:monitoring \
  --serviceaccount=kube-system:metrics-reader 2>/dev/null
cpmetrics() { docker exec lab-control-plane curl -sk -H \
  "Authorization: Bearer $(kubectl -n kube-system create token metrics-reader)" "https://127.0.0.1:$1/metrics"; }
kubeletmetrics() { kubectl get --raw "/api/v1/nodes/$1/proxy/metrics${2:-}"; }   # $2: /cadvisor, /resource
# a root shell with tcpdump/conntrack/iptables in a node's net+pid namespace
nodeshell() { docker run --rm -it --privileged --net "container:$1" --pid "container:$1" nicolaka/netshoot; }
```

Extra clusters: each is a kind `Cluster` config with `nodes: [{role: control-plane}, {role: worker}, {role: worker}]` plus the `networking` stanza below, created with `kind create cluster --name <n> --config <file>`:
- `ipvs`: `networking: {kubeProxyMode: ipvs}` · `nft`: `networking: {kubeProxyMode: nftables}` (k8s ≥ 1.31)
- `cilium`: `networking: {disableDefaultCNI: true, kubeProxyMode: none}` then `cilium install --set kubeProxyReplacement=true`
- `calico`: `networking: {disableDefaultCNI: true, podSubnet: 192.168.0.0/16}` then apply the Calico operator manifests

Shared etcd playground (chapter 04), no Kubernetes involved:

```yaml
# lab/etcd3.yaml -> docker compose -f lab/etcd3.yaml up -d
x-etcd: &etcd
  image: quay.io/coreos/etcd:v3.5.17
  entrypoint: ["/usr/local/bin/etcd"]
services:
  e1: {<<: *etcd, command: [--name=e1, --initial-advertise-peer-urls=http://e1:2380, --listen-peer-urls=http://0.0.0.0:2380, --advertise-client-urls=http://e1:2379, --listen-client-urls=http://0.0.0.0:2379, "--initial-cluster=e1=http://e1:2380,e2=http://e2:2380,e3=http://e3:2380", --initial-cluster-state=new]}
  e2: {<<: *etcd, command: [--name=e2, --initial-advertise-peer-urls=http://e2:2380, --listen-peer-urls=http://0.0.0.0:2380, --advertise-client-urls=http://e2:2379, --listen-client-urls=http://0.0.0.0:2379, "--initial-cluster=e1=http://e1:2380,e2=http://e2:2380,e3=http://e3:2380", --initial-cluster-state=new]}
  e3: {<<: *etcd, command: [--name=e3, --initial-advertise-peer-urls=http://e3:2380, --listen-peer-urls=http://0.0.0.0:2380, --advertise-client-urls=http://e3:2379, --listen-client-urls=http://0.0.0.0:2379, "--initial-cluster=e1=http://e1:2380,e2=http://e2:2380,e3=http://e3:2380", --initial-cluster-state=new]}
# client: docker compose -f lab/etcd3.yaml exec e1 etcdctl --endpoints=e1:2379,e2:2379,e3:2379 endpoint status -w table
```

---

## 00 — Linux Primitives for Containers  ([chapter](00-linux-primitives-for-containers.md))
**Time:** ~4 h · **Needs:** Linux VM (root, cgroup v2)

- [ ] **00.1 A container with no container runtime** *(Level: Core)*
  - **Goal:** see that a "container" is a process with different namespace inodes (§2, §3.1).
  - **Do:** `sudo unshare --fork --pid --net --mount --uts --ipc --cgroup --mount-proc bash`. Inside: `echo $$`, `ps -ef`, `ip link`, `hostname c1`. Outside: `lsns -p <host-pid>` and compare `readlink /proc/<host-pid>/ns/*` with `/proc/self/ns/*`.
  - **Predict:** which of the 8 namespace inodes differ from the host's, and what `ps -ef` shows if you omit `--mount-proc`.
  - **Verify:** a table in your notebook: namespace → host inode → container inode. `user` and `time` are the same (you did not unshare them).
- [ ] **00.2 cgroup v2 by hand: kill and throttle** *(Level: Core)*
  - **Goal:** enforce memory and CPU without any runtime (§3.4, §4.2–4.4).
  - **Do:** `sudo mkdir /sys/fs/cgroup/lab`; enable `+cpu +memory +pids` in the root `cgroup.subtree_control`; write `50M` to `memory.max` and `20000 100000` to `cpu.max`; move your shell in (`echo $$ > cgroup.procs`). Run `python3 -c 'b=bytearray(100*2**20)'`, then a busy loop for 10 s. (`cgcreate -g cpu,memory:lab` from libcgroup does the mkdir for you; the files are the same.)
  - **Predict:** the exit status of the Python allocation, and `nr_throttled` and `throttled_usec` after 10 s of busy loop.
  - **Verify:** `memory.events` shows `oom_kill 1`; `cpu.stat` shows about 100 periods, nearly all throttled, `throttled_usec` ≈ 8,000,000. Read `cpu.pressure` and note `some avg10`.
- [ ] **00.3 Two namespaces, one bridge** *(Level: Core)*
  - **Goal:** build the `cni0` model by hand (§10.2, §10.3, §10.8).
  - **Do:** `ip netns add a; ip netns add b`; a bridge `br0` with `10.10.0.1/24`; one veth pair per netns, host end enslaved to `br0`; addresses `.2` and `.3`. Ping a→b while `tcpdump -eni br0` runs.
  - **Predict:** which MAC addresses appear in the frames and whether the host routing table is consulted for a→b.
  - **Verify:** ping works; `bridge fdb show br br0` lists both veth MACs; `ip netns exec a ip neigh` shows b's MAC. Delete `br0` and ping fails.
- [ ] **00.4 OverlayFS copy-up and whiteouts** *(Level: Core)*
  - **Goal:** see the cost of copy-up and what a deletion stores (§9.1, §9.2).
  - **Do:** make `lower/ upper/ work/ merged/`; put a 1 GiB file in `lower`; `mount -t overlay overlay -o lowerdir=lower,upperdir=upper,workdir=work merged`. Time `echo x >> merged/big` twice. `rm merged/some-lower-file`, then `ls -l upper`.
  - **Predict:** the first append time vs the second; what type of file appears in `upper` after the delete.
  - **Verify:** the first append takes seconds (1 GiB copy-up), the second is instant; `upper` has a character device `0, 0` with the deleted name. `du -sh upper` ≈ 1 GiB.
- [ ] **00.5 Capabilities and seccomp, measured** *(Level: Core)*
  - **Goal:** see what the default container profile removes (§6.3, §7.1).
  - **Do:** `podman run --rm alpine grep Cap /proc/self/status`; decode `CapEff` with `capsh --decode=<hex>`. Repeat with `--cap-drop=ALL` and run `ping -c1 127.0.0.1`. Then `podman run --rm debian unshare --user --map-root-user true` with and without `--security-opt seccomp=unconfined`.
  - **Predict:** how many capabilities the default set holds, whether ping works with all caps dropped, and whether `unshare` works under the default seccomp profile.
  - **Verify:** capability count written down; ping fails with `Operation not permitted` (no `CAP_NET_RAW`) unless `net.ipv4.ping_group_range` allows it; `unshare` fails with `EPERM` only under the default profile.
- [ ] **00.6 An eBPF one-liner on the host** *(Level: Stretch)*
  - **Goal:** attach to a tracepoint and read a map (§11.1, §11.2).
  - **Do:** `sudo bpftrace -e 'tracepoint:syscalls:sys_enter_execve { @[comm] = count(); }'` while you start three containers with `podman run --rm alpine true`. Then `sudo bpftool prog list | tail`.
  - **Verify:** the map output lists `runc`/`crun`, `conmon` and the container's process; `bpftool` shows your program while it runs and not after.

**Checkpoint (closed book):**
1. Why does `unshare --pid` need `--fork` to give you PID 1?
2. What does `cpu.max = "20000 100000"` mean, and what does a single-threaded busy loop see?
3. How does OverlayFS record that a lower-layer file was deleted?
<details><summary>Answers</summary>

1. A PID namespace applies to the caller's children, not the caller. The forked child is the first process in the new namespace, so it is PID 1.
2. 20 ms of CPU time per 100 ms period (0.2 CPU). The loop runs 20 ms, is throttled for 80 ms, every period.
3. A whiteout: a character device with major/minor 0/0 in the upper dir (in an image layer tarball it is a `.wh.<name>` entry).
</details>

---

## 01 — Container Runtimes: CRI and OCI  ([chapter](01-container-runtimes-cri-oci.md))
**Time:** ~3 h · **Needs:** Linux VM with `runc`; kind `lab`

- [ ] **01.1 runc from a bare bundle** *(Level: Core)*
  - **Goal:** walk the OCI state machine yourself (§2.1, §2.3, §3.2).
  - **Do:** `mkdir -p b/rootfs && podman export $(podman create busybox) | tar -C b/rootfs -xf -`; `cd b && runc spec`; set `"args": ["sleep","300"]` and `"terminal": false`. `sudo runc create demo`, `sudo runc state demo`, `ps -ef | grep 'runc init'`, then `sudo runc start demo`, `sudo runc state demo`.
  - **Predict:** the status after `create` and which process exists between `create` and `start`.
  - **Verify:** `created` then `running`; between the two a `runc init` process is blocked on `exec.fifo`, and it becomes `sleep` (same PID) after `start`.
- [ ] **01.2 Edit config.json, watch runc do cgroups** *(Level: Core)*
  - **Goal:** map config.json fields to kernel state (§2.2, §3.4).
  - **Do:** add `linux.resources.memory.limit: 67108864` and `linux.resources.pids.limit: 20`, set `hostname`. Run the bundle. Find its cgroup: `cat /proc/<pid>/cgroup`, then read `memory.max` and `pids.max` there. Add a `createRuntime` hook that writes `date +%s%N` to a file.
  - **Verify:** `memory.max` = 67108864; `pids.max` = 20; the hook file exists and its timestamp is earlier than the process start time (`ps -o lstart`).
- [ ] **01.3 Map the stack on a real node** *(Level: Core)*
  - **Goal:** see kubelet → containerd → shim → runc as processes (§1.1, §6.6, §12).
  - **Do:** in `nodeshell lab-worker`: `crictl pods | wc -l`, `crictl ps | wc -l`, `ps -ef | grep -c '[c]ontainerd-shim-runc-v2'`, `runc --root /run/containerd/runc/k8s.io list | wc -l`.
  - **Predict:** is the shim count equal to the number of Pods, or of containers?
  - **Verify:** shims = sandboxes (one shim per Pod), runc list = sandboxes + containers. Write the four numbers down.
- [ ] **01.4 Restart containerd under load** *(Level: Core)*
  - **Goal:** prove workloads survive runtime restarts because of the shim (§6.6, §13).
  - **Do:** run an nginx Pod on `lab-worker`; loop `curl` against its Pod IP from another Pod every 100 ms. `docker exec lab-worker systemctl restart containerd`.
  - **Predict:** how many curl failures you get.
  - **Verify:** zero failures; the nginx PID in `crictl inspect` is unchanged; `crictl ps` is briefly unavailable during the restart.
- [ ] **01.5 Drive CRI by hand** *(Level: Stretch)*
  - **Goal:** issue the kubelet's calls yourself (§8.2, §10.2).
  - **Do:** on the node write `sandbox.json` and `container.json` (see `crictl` docs), then `crictl runp`, `crictl create`, `crictl start`, `crictl exec`. Compare the call order with `journalctl -u containerd` from a real Pod start.
  - **Verify:** your container runs inside a pause sandbox with its own netns (`crictl inspectp` shows the netns path); clean up with `crictl stopp`/`rmp`.
- [ ] **01.6 runc vs crun start latency** *(Level: Stretch)*
  - **Do:** `hyperfine -w 3 'podman --runtime runc run --rm alpine true' 'podman --runtime crun run --rm alpine true'`.
  - **Predict:** the ratio (§4.1 claims crun is faster to start).
  - **Verify:** mean and stddev for both; write the ratio down.

**Checkpoint (closed book):**
1. After `runc run` exits for a containerd-managed container, which process is the container's parent?
2. What is the difference between `runc create` and `runc start`?
3. What does `RunPodSandbox` produce on a containerd node?
<details><summary>Answers</summary>

1. `containerd-shim-runc-v2`, one per Pod. runc is a short-lived CLI; the shim holds stdio and reports exit status.
2. `create` sets up namespaces, cgroups, and rootfs, then parks `runc init` on `exec.fifo`. `start` opens the fifo, and `runc init` execs the user process.
3. A pause container that holds the Pod's network, IPC, and UTS namespaces, with the network set up by the CNI plugin, plus a shim.
</details>

---

## 02 — Container Images and Registries  ([chapter](02-container-images-and-registries.md))
**Time:** ~3 h · **Needs:** Docker, `curl`, `jq`, `crane`, local `registry:2`

- [ ] **02.1 The token dance and a manifest by curl** *(Level: Core)*
  - **Goal:** pull an image with only HTTP (§11.3, §12.1).
  - **Do:** `TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/alpine:pull" | jq -r .token)`; GET `https://registry-1.docker.io/v2/library/alpine/manifests/latest` with `Accept: application/vnd.oci.image.index.v1+json`; choose the `linux/amd64` digest; GET that manifest, then its config blob.
  - **Predict:** which object `latest` resolves to (index or manifest) and how many layers alpine has.
  - **Verify:** `sha256sum` of the raw manifest bytes equals the digest you requested it by; the config's `rootfs.diff_ids` count equals the manifest's layer count.
- [ ] **02.2 Layer dedup in a real registry** *(Level: Core)*
  - **Goal:** measure content-addressed dedup (§6.2, §6.3).
  - **Do:** `docker run -d -p 5000:5000 --name reg registry:2`. Build two images `FROM python:3.12-slim` that differ in one `COPY`; push both. `docker exec reg find /var/lib/registry -path '*blobs*' -name data | wc -l`.
  - **Predict:** the blob count (base layers + 2 unique layers + 2 configs + 2 manifests).
  - **Verify:** the count matches; `du -sh` of the registry is about one base image, not two.
- [ ] **02.3 A deletion that makes the image bigger** *(Level: Core)*
  - **Goal:** see a whiteout inside a layer tarball (§7.1).
  - **Do:** `RUN dd if=/dev/urandom of=/big bs=1M count=100` then `RUN rm /big`. Build and `docker save -o img.tar`; extract; list each layer tar with `tar -tvf` and find `.wh.big`.
  - **Predict:** the image size with and without the `rm` line.
  - **Verify:** both are ~100 MB larger than the base; the last layer contains only the `.wh.big` entry. Fix with a single `RUN` and record the new size.
- [ ] **02.4 Tags move, digests do not** *(Level: Core)*
  - **Goal:** reproduce the mutable-tag failure in a cluster (§9.1, §9.4).
  - **Do:** push `localhost:5000/app:v1` (prints "one"), run a 2-replica Deployment with `imagePullPolicy: IfNotPresent` (wire the registry into kind per the kind local-registry guide). Push new content to the same `:v1`. Delete one Pod.
  - **Predict:** what each replica prints after the delete if they land on different nodes.
  - **Verify:** `kubectl get pods -o jsonpath='{..imageID}'` shows two different digests under one tag. Pin by digest and repeat: identical.
- [ ] **02.5 Multi-arch index and reproducible layers** *(Level: Stretch)*
  - **Do:** `docker buildx build --platform linux/amd64,linux/arm64 -t localhost:5000/multi:1 --push .`; `crane manifest localhost:5000/multi:1 | jq '.manifests[].platform'`. Build twice with `SOURCE_DATE_EPOCH=0` and `--output type=image,rewrite-timestamp=true`.
  - **Verify:** two platform entries; the two builds have identical manifest digests (and differ without `SOURCE_DATE_EPOCH`).

**Checkpoint (closed book):**
1. What is the image ID, and how does it differ from the digest you pull by?
2. Why does `RUN rm` in a later layer not reduce image size?
3. What is the token scope string for pulling `library/alpine`?
<details><summary>Answers</summary>

1. The image ID is the digest of the config blob. You pull by the manifest digest, the hash of the manifest bytes that point to the config and layers.
2. Layers are immutable tarballs. The later layer only adds a whiteout entry; the bytes are still in the earlier layer, and they are still pulled.
3. `repository:library/alpine:pull`.
</details>

---

## 03 — Kubernetes Architecture Overview  ([chapter](03-kubernetes-architecture-overview.md))
**Time:** ~3 h · **Needs:** kind `lab` (destructive tasks: recreate afterwards)

- [ ] **03.1 Count discovery traffic** *(Level: Core)*
  - **Goal:** see what `kubectl` asks before it asks for Pods (§16).
  - **Do:** `rm -rf ~/.kube/cache; kubectl get pods -v=6 2>&1 | grep -c 'GET https'`; run it again.
  - **Predict:** both counts.
  - **Verify:** the cold run does many discovery GETs (`/api`, `/apis`, per-group); the warm run does one. Name the cache directory that made the difference.
- [ ] **03.2 Heartbeats are Lease writes** *(Level: Core)*
  - **Goal:** measure the node heartbeat and the leader-election leases (§10).
  - **Do:** `kubectl -n kube-node-lease get lease lab-worker -o jsonpath='{.spec.renewTime}{"\n"}' -w` for one minute; `kubectl -n kube-system get lease`.
  - **Predict:** the renew interval, and which components hold a lease in `kube-system`.
  - **Verify:** renew interval (≈10 s) written down; `kube-scheduler` and `kube-controller-manager` leases exist with `holderIdentity` = the control-plane Pod.
- [ ] **03.3 Remove one control-plane component at a time** *(Level: Core, destructive)*
  - **Goal:** observe which part of the pipeline stops (§6.3, §6.4).
  - **Do:** `docker exec lab-control-plane mv /etc/kubernetes/manifests/kube-scheduler.yaml /root/`. `kubectl create deploy web --image=nginx --replicas=3`. Restore it. Repeat with `kube-controller-manager.yaml` and a new Deployment.
  - **Predict:** for each case, which objects exist: Deployment, ReplicaSet, Pods, Pods with `nodeName`.
  - **Verify:** no scheduler: RS and Pending Pods exist, bound as soon as it returns. No controller-manager: only the Deployment exists. Running Pods keep serving throughout.
- [ ] **03.4 Kill a kubelet and time the consequences** *(Level: Core, destructive)*
  - **Goal:** measure the node-failure timeline (§5.3, §6.6).
  - **Do:** place a Deployment on `lab-worker2`. `docker exec lab-worker2 systemctl stop kubelet`. Every 5 s record node Ready status, taints, and Pod status.
  - **Predict:** time to `NotReady`, time to the `unreachable:NoExecute` taint, time until replacement Pods start elsewhere.
  - **Verify:** your three measured times against `node-monitor-grace-period` and the default 300 s `tolerationSeconds`. `docker exec lab-worker2 crictl ps` shows the "evicted" containers still running.
- [ ] **03.5 Trace one Pod through every actor** *(Level: Stretch)*
  - **Goal:** reproduce the end-to-end trace of §24 from your own logs.
  - **Do:** `kubectl run trace --image=nginx`; then `auditlog | jq -c 'select(.objectRef.name=="trace") | [.stageTimestamp,.user.username,.verb,.objectRef.subresource]'` and `kubectl get events --field-selector involvedObject.name=trace`.
  - **Verify:** an ordered list: you (create) → scheduler (`binding`) → kubelet (`status` patch), each with a timestamp. Total create-to-Running time written down.
- [ ] **03.6 Explain it** *(Level: Core)*
  - **Do:** write a 5-sentence note to an on-call engineer: "which cluster links can be down for an hour without user impact, and which cannot" (§5.9). Use your numbers from 03.3 and 03.4.

**Checkpoint (closed book):**
1. Which component writes `spec.nodeName`, and through which endpoint?
2. All apiservers are down for 10 minutes. What happens to running Pods and to new Deployments?
3. What are the two ways a kubelet reports node health?
<details><summary>Answers</summary>

1. The scheduler, through the Pod `binding` subresource (a POST to `.../pods/<name>/binding`).
2. Running Pods keep running; the kubelet keeps restarting failed containers locally. Nothing new is scheduled or reconciled, and no status is written.
3. It renews a Lease in `kube-node-lease` every ~10 s, and it updates `NodeStatus` when something changes (or every 5 minutes at most).
</details>

---

## 04 — etcd Internals  ([chapter](04-etcd-internals.md))
**Time:** ~4 h · **Needs:** kind `lab`, the `etcd3` compose playground · **First:** [k8s-learn/api-machinery-tasks.md](../k8s-learn/api-machinery-tasks.md) Level 1

- [ ] **04.1 The keyspace of a live cluster** *(Level: Core)*
  - **Goal:** see how Kubernetes lays out objects in etcd (§13).
  - **Do:** `ketcd get /registry --prefix --keys-only | grep . | cut -d/ -f3 | sort | uniq -c | sort -rn | head -15`.
  - **Predict:** the three resource types with the most keys in an idle 3-node cluster.
  - **Verify:** the top 15 written down with counts. Find the key for one Pod and one CRD instance (`/registry/<group>/<plural>/...`) and note the difference.
- [ ] **04.2 resourceVersion is a revision** *(Level: Core)*
  - **Goal:** connect `resourceVersion` to MVCC (§4.1, §4.2).
  - **Do:** `kubectl create cm rv --from-literal=a=1`; update it twice. `ketcd get /registry/configmaps/default/rv -w json | jq '.header.revision, .kvs[0].create_revision, .kvs[0].mod_revision, .kvs[0].version'`; compare with `.metadata.resourceVersion`.
  - **Predict:** which etcd field equals `resourceVersion`, and the value of `version`.
  - **Verify:** `resourceVersion` = `mod_revision`; `version` = 3; `header.revision` ≥ `mod_revision`, because the revision counter is global.
- [ ] **04.3 Replay history, then compact it away** *(Level: Core)*
  - **Goal:** produce the "required revision has been compacted" error behind the 410 Gone (§5.1, §5.5, §8.1).
  - **Do:** `ketcd watch /registry/configmaps/default/rv --rev=<create_revision>` (it replays all three versions; Ctrl-C). Get the current revision, `ketcd compact <current>`, and repeat the watch.
  - **Predict:** what the second watch prints.
  - **Verify:** the first watch prints three PUT events; the second fails with `required revision has been compacted`.
- [ ] **04.4 Leader loss and quorum loss** *(Level: Core)*
  - **Goal:** measure Raft election time and what works without quorum (§2.1, §2.3, §12.1).
  - **Do:** in the compose playground, find the leader (`endpoint status -w table`). Run a write loop `while :; do etcdctl put k $(date +%s%N) || echo FAIL; sleep 0.05; done` against the two followers. `docker compose stop <leader>`. Then stop a second member and try `get k` with and without `--consistency=s`.
  - **Predict:** how many writes fail during the election; whether each read works after quorum is lost.
  - **Verify:** the failed-write window in ms (on the order of the 1 s election timeout); with one member left, writes and linearizable reads time out, serializable reads return the last value.
- [ ] **04.5 Fill the quota, then recover** *(Level: Core)*
  - **Goal:** hit the `NOSPACE` alarm and learn why compaction alone does not fix disk size (§8.4, §9.1, §11.3).
  - **Do:** run a single etcd with `--quota-backend-bytes=33554432`. Overwrite 100 keys with 8 KiB values in a loop until writes fail with `database space exceeded`. Record `dbSize` from `endpoint status -w json`. Then: `compact <rev>` → record size → `defrag` → record size → `alarm disarm`.
  - **Predict:** the db size after compaction and after defrag.
  - **Verify:** compaction leaves the file size unchanged (`dbSizeInUse` drops); defrag shrinks the file; writes succeed only after the disarm.
- [ ] **04.6 Is your disk fast enough for etcd?** *(Level: Stretch)*
  - **Do:** `fio --rw=write --ioengine=sync --fdatasync=1 --directory=/var/tmp/etcdtest --size=22m --bs=2300 --name=etcd` (§11.7).
  - **Verify:** p99 `fdatasync` latency written down; compare with the chapter's 10 ms guidance. Run it on your laptop disk and a tmpfs and explain the difference.

**Checkpoint (closed book):**
1. What is a Pod's `resourceVersion` in etcd terms, and what is a list's `resourceVersion`?
2. Why does compaction not shrink the etcd database file?
3. A 3-member cluster loses 2 members. What still works?
<details><summary>Answers</summary>

1. For an object it is the key's `mod_revision`. For a list it is the store's `header.revision` at the time the list was read.
2. Compaction removes old revisions and frees pages inside bbolt, but the file keeps its size. Only defrag rewrites the file.
3. Nothing that needs consensus: no writes, no linearizable reads. Serializable (`--consistency=s`) reads from the surviving member still return possibly stale data.
</details>

---

## 05 — kube-apiserver Internals  ([chapter](05-kube-apiserver-internals.md))
**Time:** ~4 h · **Needs:** kind `lab` · **First:** [k8s-learn/api-machinery-tasks.md](../k8s-learn/api-machinery-tasks.md) Levels 1, 4, 5

- [ ] **05.1 Follow a request through the audit log** *(Level: Core)*
  - **Goal:** see the handler chain's outputs for one object (§3, §12).
  - **Do:** `kubectl run a1 --image=nginx`. `auditlog | jq -c 'select(.objectRef.name=="a1") | [.stage,.verb,.user.username,.objectRef.subresource,.responseStatus.code,.userAgent]'`.
  - **Predict:** how many distinct users touch the Pod in its first 10 s, and with which verbs and subresources.
  - **Verify:** at least: your `create`, the scheduler's `create` on `binding`, the kubelet's `patch` on `status`. Get the `annotations` of one event and find the authorization decision and reason.
- [ ] **05.2 Protobuf vs JSON on the wire** *(Level: Core)*
  - **Goal:** measure the encoding difference (§6.1).
  - **Do:** `kubectl proxy &`; `curl -s localhost:8001/api/v1/pods | wc -c` vs the same with `-H 'Accept: application/vnd.kubernetes.protobuf'`. Time both with 200 Pods in the cluster.
  - **Predict:** the size ratio.
  - **Verify:** the ratio and the timings written down. Built-in types are served as protobuf; try the same on a CRD and get JSON.
- [ ] **05.3 What is actually stored** *(Level: Core)*
  - **Goal:** see storage encoding and storage version (§6.2, §6.3).
  - **Do:** `ketcd get /registry/pods/default/a1 --print-value-only | head -c 48 | xxd`. Create an HPA with `autoscaling/v2`, read it back as `autoscaling/v1` with `kubectl get hpa.v1.autoscaling`, and `ketcd get /registry/horizontalpodautoscalers/default/<name> --print-value-only | strings | head -3`.
  - **Predict:** the first bytes of a stored Pod, and the version the HPA is stored in.
  - **Verify:** the `k8s\0` magic followed by `v1`/`Pod`; the HPA is stored as `autoscaling/v2` no matter which version you read.
- [ ] **05.4 APF: throttle a noisy client** *(Level: Core)*
  - **Goal:** watch Priority and Fairness isolate a flood (§11).
  - **Do:** create a `PriorityLevelConfiguration` `lab-low` (Limited, `nominalConcurrencyShares: 1`, queuing with 4 queues, `queueLengthLimit: 5`) and a `FlowSchema` that sends ServiceAccount `default:noisy` to it; bind the `view` ClusterRole to `noisy` (authorization runs before APF, so a 403 never reaches the queues). Flood: `seq 300 | xargs -P50 -I{} kubectl --as=system:serviceaccount:default:noisy get --raw /api/v1/pods >/dev/null`. Time your own `kubectl get pods` during the flood.
  - **Predict:** how many noisy requests get 429, and whether your latency changes.
  - **Verify:** `kubectl get --raw /metrics | grep 'apiserver_flowcontrol_rejected_requests_total{.*lab-low'` is > 0; your own latency stays flat. `kubectl get pods -v=8` shows the `X-Kubernetes-PF-FlowSchema-UID` response header.
- [ ] **05.5 Watch cache vs quorum read, measured** *(Level: Stretch)*
  - **Do:** create 3000 ConfigMaps. Time 20 runs each of `kubectl get --raw '/api/v1/configmaps?resourceVersion=0'` and `kubectl get --raw /api/v1/configmaps` (§7.4, §8.1). Read `apiserver_watch_cache_*` and `apiserver_storage_list_*` metrics before and after.
  - **Verify:** p50/p95 for both paths, and which metric counter moved for each. Explain any difference with your Kubernetes version's consistent-list-from-cache behavior (§7.10).

**Checkpoint (closed book):**
1. Put these in chain order: authorization, authentication, mutating admission, APF, validating admission, impersonation.
2. Which version is an object stored in when you write it with a non-storage version?
3. How does APF protect one client's traffic from another's?
<details><summary>Answers</summary>

1. Authentication → impersonation → authorization → APF → mutating admission → (schema validation) → validating admission.
2. The resource's storage version. The apiserver converts through the internal hub version on the way in and out.
3. FlowSchemas map requests to priority levels with their own concurrency shares, and within a level, flows (such as per user) are shuffle-sharded into queues. A flood fills its own queues and gets 429s while other flows keep their seats.
</details>

---

## 06 — Admission Control Deep Dive  ([chapter](06-admission-control-deep-dive.md))
**Time:** ~5 h · **Needs:** kind `lab`, Go or Python, `openssl` or cert-manager

- [ ] **06.1 A mutating webhook that injects a label** *(Level: Core)*
  - **Goal:** write the AdmissionReview round trip yourself (§3, §7.4, §9).
  - **Do:** a small HTTPS server (Python stdlib `http.server` + `ssl`, or Go `net/http`) that answers `/mutate` with `allowed: true`, `patchType: JSONPatch`, and a base64 JSON Patch adding `metadata.labels.injected=true`. Deploy it with a Service; sign its cert with your own CA; set `caBundle`; scope it with `namespaceSelector: {matchLabels: {webhook: on}}`.
  - **Predict:** what happens to a Pod whose `metadata.labels` is absent. Your patch op must handle that.
  - **Verify:** new Pods in the labelled namespace carry `injected=true`; the webhook log shows one request per Pod create with `dryRun: false`; `kubectl create --dry-run=server` also calls it.
- [ ] **06.2 Measure the latency you added** *(Level: Core)*
  - **Goal:** put a number on the webhook tax (§8.2).
  - **Do:** add `time.sleep(0.3)` in the handler. Time 50 Pod creates with and without the webhook. Read `apiserver_admission_webhook_admission_duration_seconds_bucket{name="..."}` from `kubectl get --raw /metrics`.
  - **Predict:** p50 create latency with a 300 ms webhook.
  - **Verify:** both p50s written down; the histogram shows the webhook's own share.
- [ ] **06.3 Wedge the cluster with `failurePolicy: Fail`** *(Level: Core, destructive)*
  - **Goal:** reproduce the classic outage and its escape hatch (§4.6, §11.2, §11.3).
  - **Do:** widen the webhook to all namespaces (no selector), `failurePolicy: Fail`, and scale the webhook Deployment to 0.
  - **Predict:** whether you can scale the webhook back up.
  - **Verify:** Pod creates fail with `failed calling webhook`, including the webhook's own Pods. Recover by deleting the `MutatingWebhookConfiguration`. Write down the selector that would have prevented it (exclude its own namespace and `kube-system`).
- [ ] **06.4 The same rule as a ValidatingAdmissionPolicy** *(Level: Core)*
  - **Goal:** move a check in-process with CEL (§13).
  - **Do:** a VAP rejecting Deployments with `replicas > 5` in namespaces labelled `env=dev`, with a binding using `validationActions: [Deny]`. Then switch to `[Warn, Audit]`.
  - **Verify:** `Deny` blocks with your message; `Warn` returns a kubectl warning and the audit log event carries the `validation.policy.admission.k8s.io/validation_failure` annotation. No Pod, no TLS, no network hop.
- [ ] **06.5 Two mutators, one field** *(Level: Stretch)*
  - **Goal:** see ordering and reinvocation (§6, §6.1).
  - **Do:** two webhooks: A adds a sidecar container; B sets `resources.limits` on every container. Register B's configuration name so it sorts before A. Create a Pod with `reinvocationPolicy: Never`, then `IfNeeded` on B.
  - **Predict:** whether A's sidecar gets limits in each case.
  - **Verify:** `Never`: the sidecar has no limits. `IfNeeded`: it does, and B's log shows two calls for one create.

**Checkpoint (closed book):**
1. Why must a mutating webhook's patch be idempotent?
2. What two settings together caused the self-fencing wedge in 06.3?
3. Name one thing VAP cannot do that a webhook can.
<details><summary>Answers</summary>

1. The apiserver may call it more than once (reinvocation, retries, dry-run), and other mutators may already have applied the same change.
2. `failurePolicy: Fail` and rules that match the webhook's own Pods (no namespace or object selector excluding them).
3. Call out to external systems or state (for example a registry or a database). VAP is limited to CEL over the request, the object, params, and namespace data. Validating policies also cannot mutate; that needs MutatingAdmissionPolicy or a webhook.
</details>

---

## 07 — Authentication and Authorization  ([chapter](07-authentication-authorization.md))
**Time:** ~3 h · **Needs:** kind `lab`, `openssl`, `jq`

- [ ] **07.1 Mint a user with the CSR API** *(Level: Core)*
  - **Goal:** see how identity is encoded in x509 (§4.1, §29).
  - **Do:** `openssl req -new -newkey rsa:2048 -nodes -keyout alice.key -subj "/CN=alice/O=dev-team" -out alice.csr`; submit a `CertificateSigningRequest` with `signerName: kubernetes.io/kube-apiserver-client`; approve it; build a kubeconfig.
  - **Predict:** `kubectl auth whoami` output and whether alice can `get pods`.
  - **Verify:** username `alice`, groups `dev-team` and `system:authenticated`; `Forbidden` until you bind a Role to the group `dev-team`, not to the user.
- [ ] **07.2 Decode and scope a projected token** *(Level: Core)*
  - **Goal:** read a bound ServiceAccount token's claims (§8.1, §8.2).
  - **Do:** `kubectl exec` into a Pod and decode the payload of `/var/run/secrets/kubernetes.io/serviceaccount/token` (`cut -d. -f2 | base64 -d`, fix padding). Then `kubectl create token default --audience=vault --duration=10m` and decode that.
  - **Predict:** `exp - iat` for the mounted token, and whether the `vault` token works against the apiserver.
  - **Verify:** the mounted token carries `kubernetes.io.pod` with the Pod's name and UID, and `exp - iat` is about a year with a `warnafter` claim about an hour out (the kubelet asks for 3607 s; the apiserver extends it for old clients); the `vault`-audience token gets `401` from the apiserver (`curl -k -H "Authorization: Bearer ..."`). Delete the Pod: its token is rejected within seconds.
- [ ] **07.3 RBAC evaluation, observed** *(Level: Core)*
  - **Goal:** confirm RBAC is a union of allows, and that `list` leaks what `get` protects (§19.5, §20).
  - **Do:** create Pods `x` and `y` in `dev`. Give alice Role A: `get pods` with `resourceNames: [x]`. Test `kubectl get pod x` and `kubectl get pod y` as alice. Then add Role B: `list pods` (no names) and run `kubectl get pods -o yaml --as=alice -n dev`.
  - **Predict:** each of the three results.
  - **Verify:** `get x` allowed, `get y` forbidden, and the list returns the full spec of `y`. The audit log shows `authorization.k8s.io/reason` naming the RoleBinding that allowed each call.
- [ ] **07.4 The escalation guard** *(Level: Core)*
  - **Goal:** hit the RBAC escalation check (§19.7).
  - **Do:** give alice `create` on `roles` and `rolebindings` in `dev`, but only `get pods`. As alice, create a Role that grants `delete pods` and bind it to herself.
  - **Predict:** which of the two creates fails.
  - **Verify:** the Role create fails with `attempting to grant RBAC permissions not currently held`. Add `escalate` on `roles` and retry.
- [ ] **07.5 Impersonation leaves a trail** *(Level: Stretch)*
  - **Do:** as admin, `kubectl get pods --as=alice --as-group=dev-team`. Find the event in the audit log.
  - **Verify:** the event has `user` = you and `impersonatedUser` = alice. Write one sentence on why the `impersonate` verb is as dangerous as cluster-admin (§26).

**Checkpoint (closed book):**
1. Where in an x509 client cert does Kubernetes read the username and groups?
2. What three things bind a projected ServiceAccount token?
3. Can an RBAC rule deny access?
<details><summary>Answers</summary>

1. CN is the username; each O (organization) is a group.
2. An audience, an expiry, and a bound object (the Pod, and through it the ServiceAccount). If the Pod is deleted, the token is rejected.
3. No. RBAC is purely additive. A request is allowed if any binding allows it; if none does, the authorizer gives no opinion and the request ends up denied.
</details>

---

## 08 — The Controller Pattern and client-go  ([chapter](08-controller-pattern-and-client-go.md))
**Time:** ~5 h · **Needs:** kind `lab`, Go (client-go or controller-runtime) and Python (`kopf`) · **First:** [k8s-learn/controller-tasks.md](../k8s-learn/controller-tasks.md) Levels 2–5

- [ ] **08.1 Poller vs informer, counted in the audit log** *(Level: Core)*
  - **Goal:** put a number on why informers exist (§1, §4).
  - **Do:** for 60 s run a poller (`while :; do kubectl get pods -A -o name >/dev/null; sleep 1; done`), then for 60 s a minimal informer (client-go `SharedInformerFactory` on Pods, or Python `kubernetes.watch`). Give each a distinct user agent (`--user-agent` / `rest.Config.UserAgent`). Count `list` events per user agent in `auditlog`.
  - **Predict:** both counts.
  - **Verify:** ~60 LISTs for the poller, 1 LIST (+ 1 long-running WATCH, which this audit policy drops) for the informer.
- [ ] **08.2 Measure the default rate limiter curve** *(Level: Core)*
  - **Goal:** see the workqueue's per-item backoff (§8).
  - **Do:** a reconciler that always returns an error for one key and logs a timestamp on each call. Let it run 3 minutes. Tabulate the gaps between calls.
  - **Predict:** the first 8 gaps (the chapter's per-item exponential limiter: base and cap).
  - **Verify:** gaps of 5 ms, 10 ms, 20 ms … doubling; `workqueue_retries_total{name="..."}` matches your call count − 1. Return success once: the gap resets on the next failure only if you call `Forget`.
- [ ] **08.3 The same controller in kopf** *(Level: Core)*
  - **Goal:** compare a Python framework's retry and state model with client-go's.
  - **Do:** `uv add kopf kubernetes`; a `@kopf.on.create('configmaps', labels={'lab': 'kopf'})` handler that raises on the first 3 calls, then succeeds. Run with `uv run kopf run ctl.py --verbose`.
  - **Predict:** the gap between retries, and where kopf keeps its retry and progress state.
  - **Verify:** the measured gaps; `kubectl get cm <x> -o yaml` shows `kopf.zalando.org/*` annotations. The state lives on the object, not in an in-memory queue. One sentence: what does that cost at 10k objects?
- [ ] **08.4 Leader election failover time** *(Level: Core)*
  - **Goal:** measure the Lease handoff (§10).
  - **Do:** run 2 replicas with leader election (`LeaseDuration 15s, RenewDeadline 10s, RetryPeriod 2s`). Watch `kubectl get lease <name> -o jsonpath='{.spec.holderIdentity}' -w`. Kill the leader with `--grace-period=0 --force`. Then repeat with a graceful delete and `ReleaseOnCancel: true`.
  - **Predict:** failover time in both cases.
  - **Verify:** forced ≈ lease duration + up to one retry period; graceful release is about one retry period or less.
- [ ] **08.5 Restart the apiserver under a running informer** *(Level: Stretch)*
  - **Goal:** see the reflector's relist/resume path (§4, §5).
  - **Do:** run your informer with `-v=4` (klog), bounce the apiserver (`docker exec lab-control-plane mv` its manifest out and back). Count LISTs from your user agent in `auditlog` and read the reflector log lines.
  - **Verify:** whether it resumed the watch from the last resourceVersion or relisted, and why (watch closed vs 410). Your handlers saw no duplicate adds if the cache diff worked.

**Checkpoint (closed book):**
1. What does the workqueue guarantee about a single key?
2. What is client-go's default controller rate limiter?
3. What bounds the time two replicas might both think they are leader?
<details><summary>Answers</summary>

1. It is deduplicated while waiting, never processed by two workers at once, and re-queued once after `Done` if it was added again while processing.
2. The max of a per-item exponential backoff (5 ms doubling to a 1000 s cap) and an overall token bucket (10 qps, burst 100).
3. The old leader stops acting when it fails to renew within `RenewDeadline`, which is shorter than `LeaseDuration`, the time a candidate must wait before taking over. Clock skew between nodes eats into that margin.
</details>

---

## 09 — kube-scheduler Internals  ([chapter](09-kube-scheduler-internals.md))
**Time:** ~4 h · **Needs:** kind `lab`, `kwokctl` for the stretch · **First:** [k8s-learn/scheduling-constraints-tasks.md](../k8s-learn/scheduling-constraints-tasks.md) Levels 1–5

- [ ] **09.1 Read the scheduler's arithmetic** *(Level: Core)*
  - **Goal:** see Filter and Score per node (§3, §6).
  - **Do:** raise the scheduler to `--v=10` (`sed` into `/etc/kubernetes/manifests/kube-scheduler.yaml`). Put a 1-CPU Pod on `lab-worker`, then create a 200m Pod with no constraints. Grep the scheduler log for that Pod's per-plugin scores.
  - **Predict:** which node wins, and which plugin decides it.
  - **Verify:** a table of plugin × node scores from the log; the winner matches the highest weighted sum. Set `--v` back to 2.
- [ ] **09.2 The unschedulable queue and what wakes it** *(Level: Core)*
  - **Goal:** watch a Pod move between queues (§4).
  - **Do:** create a Pod with `nodeSelector: {disk: ssd}`. Read `scheduler_pending_pods` via `cpmetrics 10259`. Then `kubectl label node lab-worker disk=ssd` and time until `.spec.nodeName` is set.
  - **Predict:** which queue it sits in, and the wake-up delay after the label (seconds, or the 5-minute flush).
  - **Verify:** `queue="unschedulable"` = 1 before, 0 after; measured delay in seconds (the node-label event requeues it).
- [ ] **09.3 Scheduling gates** *(Level: Core)*
  - **Do:** create a Pod with `schedulingGates: [{name: lab/quota}]`; check `scheduler_pending_pods{queue="gated"}` and `kubectl get pod` (`SchedulingGated`). Remove the gate with a JSON patch (§11).
  - **Verify:** gated count 1 → 0; the scheduler never attempted the Pod while gated (`scheduler_schedule_attempts_total` unchanged until removal).
- [ ] **09.4 Spread vs bin-pack, side by side** *(Level: Core)*
  - **Goal:** see a scoring strategy change placement (§13, §18).
  - **Do:** run a second scheduler as a Deployment (the `kube-scheduler` image, a ConfigMap with a `KubeSchedulerConfiguration` profile `binpack` whose `NodeResourcesFit` uses `scoringStrategy: {type: MostAllocated}`, leader election off, RBAC from `system:kube-scheduler`). Create 6 Pods of 200m CPU with the default scheduler, then 6 with `schedulerName: binpack`.
  - **Predict:** the per-node distribution for each set.
  - **Verify:** default spreads roughly 3/3; `binpack` stacks on one node until it is full. `kubectl get events` shows which scheduler bound each Pod.
- [ ] **09.5 Throughput with 500 fake nodes** *(Level: Stretch)*
  - **Goal:** measure scheduling rate and the cost of inter-pod anti-affinity (§7, §14).
  - **Do:** `kwokctl create cluster`, `kwokctl scale node --replicas 500`; create a 5000-replica Deployment; time until no Pod is Pending. Repeat with a preferred `podAntiAffinity` on hostname.
  - **Predict:** Pods per second in both runs.
  - **Verify:** two throughput numbers and `scheduler_scheduling_attempt_duration_seconds` p99 for each.

**Checkpoint (closed book):**
1. List the scheduling-cycle extension points in order.
2. Why is the binding cycle asynchronous?
3. What moves a Pod from the unschedulable queue back to the active queue?
<details><summary>Answers</summary>

1. PreEnqueue, QueueSort, PreFilter, Filter, PostFilter (only on failure), PreScore, Score, NormalizeScore, Reserve, Permit. Then the binding cycle: WaitOnPermit, PreBind, Bind, PostBind.
2. The scheduling cycle is serial. The scheduler assumes the Pod onto the node in its cache and binds in a goroutine, so slow volume or API work does not block the next Pod.
3. A cluster event that a plugin's queueing hint says could make it schedulable (node added or labelled, Pod deleted, and so on), or the periodic flush after `podMaxInUnschedulablePodsDuration` (5 min).
</details>

---

## 10 — kubelet Internals  ([chapter](10-kubelet-internals.md))
**Time:** ~4 h · **Needs:** kind `lab`

- [ ] **10.1 How fast does the kubelet notice a dead container?** *(Level: Core)*
  - **Goal:** measure PLEG detection (§5).
  - **Do:** in `nodeshell lab-worker`, `kill -9` the main process of a Pod's container (PID from `crictl inspect`). Run `kubectl get pod -w` with timestamps (`--output-watch-events` and `ts`). Read `kubelet_pleg_relist_duration_seconds` from `kubeletmetrics lab-worker` and check the `EventedPLEG` feature gate in `configz`.
  - **Predict:** the delay between kill and `restartCount` incrementing.
  - **Verify:** measured delay (≈1 s relist with generic PLEG; near-instant if evented PLEG is on). State which PLEG your node runs.
- [ ] **10.2 Static Pods and their mirrors** *(Level: Core)*
  - **Goal:** see the file source win over the API (§2, §25).
  - **Do:** `docker cp static.yaml lab-worker:/etc/kubernetes/manifests/`. Find the mirror Pod (`name-lab-worker`) and its `kubernetes.io/config.mirror` annotation. `kubectl delete pod` it. Then edit the image in the file.
  - **Predict:** what the delete does to the running container, and what the file edit does.
  - **Verify:** the delete recreates the mirror, and the container ID and start time are unchanged. The file edit replaces the container. Removing the file removes the Pod.
- [ ] **10.3 Recompute allocatable from the live config** *(Level: Core)*
  - **Goal:** connect kubelet config to what the scheduler sees (§16, and ch 21 §22).
  - **Do:** `kubectl get --raw /api/v1/nodes/lab-worker/proxy/configz | jq '.kubeletconfig | {kubeReserved, systemReserved, evictionHard, maxPods, podPidsLimit}'`.
  - **Predict:** `.status.allocatable.memory` from `.status.capacity.memory` and those values.
  - **Verify:** your arithmetic matches to the KiB, or you can name the missing term.
- [ ] **10.4 What probes cost the node** *(Level: Core)*
  - **Goal:** measure the probe manager's overhead (§8).
  - **Do:** deploy 40 Pods on `lab-worker` with an `exec` liveness probe (`cat /tmp/ok`) at `periodSeconds: 1`. Sample the kubelet's and containerd's CPU (`kubeletmetrics lab-worker | grep process_cpu_seconds_total`, and `top` in `nodeshell`) over 60 s. Repeat with an `httpGet` probe.
  - **Predict:** which probe type costs more, and roughly by what factor.
  - **Verify:** CPU-seconds per minute for both runs. Each `exec` probe is a CRI `ExecSync`, a runc exec in the container.
- [ ] **10.5 Status writes from a flapping probe** *(Level: Stretch)*
  - **Do:** a Pod whose readiness flips every 10 s. Count the kubelet's `patch` calls on `pods/status` for it in `auditlog` over 5 minutes (§9).
  - **Verify:** writes ≈ transitions, not probe executions; explain with the status manager's diffing.

**Checkpoint (closed book):**
1. What does PLEG do, and what happens when a relist takes longer than 3 minutes?
2. What happens when you delete a mirror Pod?
3. How is `allocatable` computed?
<details><summary>Answers</summary>

1. It relists containers from the runtime and turns state changes into Pod lifecycle events for the sync loop. A relist over the 3-minute threshold marks the node NotReady ("PLEG is not healthy").
2. The kubelet recreates the mirror. The static Pod keeps running because the manifest file is the source of truth.
3. `capacity − kube-reserved − system-reserved − hard eviction threshold`.
</details>

---

## 11 — Pod Internals  ([chapter](11-pod-internals.md))
**Time:** ~3 h · **Needs:** kind `lab` · **First:** [k8s-learn/pod-tasks.md](../k8s-learn/pod-tasks.md) Levels 2, 4, 6

- [ ] **11.1 Which namespaces are really shared** *(Level: Core)*
  - **Goal:** prove the Pod sharing model with inode numbers (§2, §3).
  - **Do:** a two-container Pod. In `nodeshell`, get the PIDs of the pause, c1 and c2 processes (`crictl inspectp` / `crictl inspect` → `.info.pid`) and `readlink /proc/<pid>/ns/{net,ipc,uts,pid,mnt}` for each. Then add `shareProcessNamespace: true` and `ps` inside c1.
  - **Predict:** a 3×5 table of same/different before you look.
  - **Verify:** net, ipc and uts are identical across all three; pid and mnt differ. With the shared PID namespace, `ps` shows `/pause` as PID 1.
- [ ] **11.2 Native sidecar start and stop order** *(Level: Core)*
  - **Goal:** measure the sidecar lifecycle (§7, §14).
  - **Do:** `initContainers: [init-a, sidecar (restartPolicy: Always, startupProbe), init-b]`, plus `app`. Each logs `date +%s.%N` at start and in a `trap ... TERM`. Delete the Pod.
  - **Predict:** start order and stop order.
  - **Verify:** start: init-a → sidecar (started) → init-b → app. Stop: app gets SIGTERM first; the sidecar gets it after app has exited.
- [ ] **11.3 Termination timeline to the millisecond** *(Level: Core)*
  - **Goal:** measure the sequence of §14.
  - **Do:** `preStop: sleep 5`; the app traps TERM and exits 3 s later; `terminationGracePeriodSeconds: 10`. Delete and record timestamps. Then make the app ignore TERM.
  - **Predict:** total time and exit code in both runs.
  - **Verify:** run 1 ≈ 8 s, exit 0. Run 2 ≈ 10 s then SIGKILL, exit 137. The grace period includes the preStop time.
- [ ] **11.4 The CrashLoopBackOff curve** *(Level: Core)*
  - **Do:** `command: ["sh","-c","exit 1"]`. For 7 restarts, record `lastState.terminated.finishedAt` and the next `state.running.startedAt` (§11).
  - **Predict:** the delays.
  - **Verify:** about 10, 20, 40, 80, 160, 300, 300 s (unless your version changes the backoff defaults; check the feature gates).
- [ ] **11.5 Kill the pause container** *(Level: Stretch)*
  - **Goal:** see what the sandbox anchors (§2).
  - **Do:** in `nodeshell`, `kill -9` the pause PID of a running Pod.
  - **Predict:** what happens to the app containers and the Pod IP.
  - **Verify:** the kubelet recreates the sandbox and restarts every container; compare the Pod IP and `restartCount` before and after.

**Checkpoint (closed book):**
1. Which namespaces do a Pod's containers share by default?
2. How does a native sidecar differ from a regular init container?
3. Does `terminationGracePeriodSeconds` include the preStop hook?
<details><summary>Answers</summary>

1. Network, IPC and UTS, all held by the pause container. PID only with `shareProcessNamespace`; mount is never shared (volumes are mounted into each).
2. It has `restartPolicy: Always`. It starts in init order, but only its start (or startupProbe) blocks the next init; it keeps running beside the app, is restarted if it dies, and is stopped after the main containers.
3. Yes. The preStop hook and the SIGTERM wait share one budget.
</details>

---

## 12 — Workload Controllers  ([chapter](12-workload-controllers.md))
**Time:** ~3 h · **Needs:** kind `lab` · **First:** [k8s-learn/deployment-tasks.md](../k8s-learn/deployment-tasks.md) Levels 2–5, [replica-tasks.md](../k8s-learn/replica-tasks.md) Level 3, [workload-controllers-tasks.md](../k8s-learn/workload-controllers-tasks.md) Levels 2–4

- [ ] **12.1 Rolling-update math, observed** *(Level: Core)*
  - **Goal:** check the surge/unavailable rounding rules (§7).
  - **Do:** 10 replicas, `maxSurge: 25%`, `maxUnavailable: 25%`, `minReadySeconds: 5`, a readiness probe. Change the image while a script samples total Pods and available replicas every 0.5 s.
  - **Predict:** the max total Pods and the min available.
  - **Verify:** max 13 (surge rounds up), min 8 (unavailable rounds down), from your samples.
- [ ] **12.2 The pod-template-hash decides** *(Level: Core)*
  - **Goal:** see how a Deployment matches ReplicaSets (§6, §9).
  - **Do:** (a) add a label to the Deployment's own metadata; (b) add an annotation to the Pod template; (c) remove it again. After each, `kubectl get rs -L pod-template-hash` and read `deployment.kubernetes.io/revision`.
  - **Predict:** which steps create a new RS, and what (c) does to revision numbers.
  - **Verify:** (a) nothing; (b) a new RS; (c) the old RS is scaled back up (same hash), and its revision number jumps to the newest.
- [ ] **12.3 Slow-start batch creation** *(Level: Core)*
  - **Goal:** see the ReplicaSet's exponential create batches (§4).
  - **Do:** a namespace with a `ResourceQuota` of `pods: 3`; a ReplicaSet with 20 replicas. From `auditlog`, list Pod `create` calls by the replicaset-controller with timestamps and response codes.
  - **Predict:** how many creates it attempts in the first sync.
  - **Verify:** batches of 1, 2, 4 … that stop at the first rejection, not 20 rejected calls per sync.
- [ ] **12.4 Job tracking finalizers** *(Level: Core)*
  - **Goal:** see how Jobs count Pods reliably (§21).
  - **Do:** a Job with `completions: 6, parallelism: 2`, each Pod sleeping 20 s. Watch `kubectl get pods -o custom-columns=N:.metadata.name,F:.metadata.finalizers`. Mid-run, stop the controller-manager (move its manifest), wait until 2 Pods finish, then restore it.
  - **Predict:** the finalizers on finished Pods while the controller is down, and `status.succeeded` after it returns.
  - **Verify:** finished Pods keep `batch.kubernetes.io/job-tracking` until counted; after the restart the count is exact and the finalizers are removed.
- [ ] **12.5 DaemonSet surge rollout** *(Level: Stretch)*
  - **Do:** a DaemonSet with `updateStrategy.rollingUpdate: {maxSurge: 1, maxUnavailable: 0}` and a readiness probe; roll the image while curling each node's Pod through a NodePort (§15).
  - **Verify:** per node, old and new Pods overlap and curl never fails; with `maxUnavailable: 1, maxSurge: 0` you see a gap. Count failures in both runs.

**Checkpoint (closed book):**
1. With 10 replicas, 25% surge and 25% unavailable, what are the bounds during a rollout?
2. How does a Deployment decide that an existing ReplicaSet matches its template?
3. Why do Job Pods carry a finalizer?
<details><summary>Answers</summary>

1. At most 13 Pods (surge 2.5 rounds up to 3); at least 8 available (unavailable 2.5 rounds down to 2).
2. It compares Pod templates; the `pod-template-hash` label (a hash of the template) names and selects the matching ReplicaSet.
3. So the Job controller counts every finished Pod before the Pod can be deleted. Status stays exact even if the controller is down or Pods are garbage-collected.
</details>

---

## 13 — StatefulSet Deep Dive  ([chapter](13-statefulset-deep-dive.md))
**Time:** ~3 h · **Needs:** kind `lab` · **First:** [k8s-learn/workload-controllers-tasks.md](../k8s-learn/workload-controllers-tasks.md) Level 1

- [ ] **13.1 ControllerRevisions are the history** *(Level: Core)*
  - **Goal:** see where StatefulSet revisions live (§8, §9).
  - **Do:** a 3-replica StatefulSet; change the image twice; `kubectl get controllerrevisions -l app=web`; read `.status.currentRevision` and `.status.updateRevision` during a rollout. `kubectl rollout undo sts/web`.
  - **Predict:** the number of ControllerRevisions after the undo.
  - **Verify:** the count, and whether the undo reused the old ControllerRevision with a bumped `.revision` or created a new object. `currentRevision` = `updateRevision` once the rollout finishes.
- [ ] **13.2 The stuck rollout** *(Level: Core)*
  - **Goal:** reproduce the OrderedReady trap (§9).
  - **Do:** update to a non-existent image tag. When `web-2` is in `ImagePullBackOff`, revert the template to the good image.
  - **Predict:** does the controller fix `web-2` on its own?
  - **Verify:** it stays broken: the controller waits for the broken Pod to become Ready. Delete `web-2` by hand; the rollout completes. Write the one-line runbook.
- [ ] **13.3 A node-pinned volume** *(Level: Core)*
  - **Goal:** see why local storage anchors Pod identity to a node (§17, §23).
  - **Do:** with kind's `standard` (local-path) class, find the node of `web-0`'s PV (`.spec.nodeAffinity`). `kubectl cordon` that node and delete `web-0`.
  - **Predict:** where `web-0` goes.
  - **Verify:** Pending with `volume node affinity conflict`. Uncordon to recover. One sentence on what a zonal cloud disk changes about this.
- [ ] **13.4 Bootstrap a 3-member etcd from DNS** *(Level: Stretch)*
  - **Goal:** build the predictable-DNS bootstrap of §14.
  - **Do:** a headless Service `etcd` and a StatefulSet running `quay.io/coreos/etcd` with `--initial-cluster=etcd-0=http://etcd-0.etcd:2380,etcd-1=...,etcd-2=...` and a readiness probe on `/health`. First with `podManagementPolicy: OrderedReady` and no `publishNotReadyAddresses`, then with `Parallel` and `publishNotReadyAddresses: true`.
  - **Predict:** which configuration deadlocks.
  - **Verify:** the first never gets `etcd-0` Ready (it waits for peers that are never created); the second forms a cluster: `etcdctl member list` shows 3 members.

**Checkpoint (closed book):**
1. In what order does a StatefulSet roll out and scale down?
2. Why do clustered databases need `publishNotReadyAddresses: true` on the headless Service?
3. After you fix a bad template, why can the rollout stay stuck?
<details><summary>Answers</summary>

1. Highest ordinal first for both.
2. Without it, DNS records exist only for Ready Pods, but members only become Ready after they have found each other. With it, peers can resolve each other before quorum.
3. With `OrderedReady`, the controller waits for the broken Pod to become Ready before it continues, and it does not replace that Pod. You must delete it.
</details>

---

## 14 — Services and kube-proxy  ([chapter](14-services-and-kube-proxy.md))
**Time:** ~4 h · **Needs:** kind `lab`, `ipvs` and `nft` clusters · **First:** [k8s-learn/service-networking-tasks.md](../k8s-learn/service-networking-tasks.md) Levels 1, 2, 4

- [ ] **14.1 Read the iptables program for one Service** *(Level: Core)*
  - **Goal:** find the random-selection chain (§7, §11).
  - **Do:** a Service with 3 endpoints. `docker exec lab-worker iptables-save -t nat | grep -A4 "KUBE-SVC-.*<ns>/<svc>"`.
  - **Predict:** the `--probability` value on each of the three `KUBE-SEP` jumps.
  - **Verify:** 0.333…, 0.5, then an unconditional jump. Scale to 4 and predict again before checking.
- [ ] **14.2 Rule explosion, measured** *(Level: Core)*
  - **Goal:** see how iptables mode scales with endpoints (§7, §25).
  - **Do:** a selectorless Service plus hand-written `EndpointSlice`s with N fake endpoints (up to 1000 per slice), for N = 100, 1000, 5000. For each: `iptables-save | wc -l` on a node and `kubeproxy_sync_proxy_rules_duration_seconds` from `docker exec lab-worker curl -s localhost:10249/metrics`. Repeat in the `ipvs` cluster with `ipvsadm -Ln | wc -l` (`apt-get install -y ipvsadm` in the node).
  - **Predict:** growth of rule count and sync time in both modes.
  - **Verify:** a table N → lines → sync p50 for iptables; IPVS keeps the iptables count flat and grows only the IPVS table.
- [ ] **14.3 Ping a ClusterIP** *(Level: Core)*
  - **Goal:** confirm a ClusterIP is not an interface in iptables mode (§1, §8).
  - **Do:** `ping -c2 <clusterIP>` from a Pod in `lab`, then in `ipvs`. In `ipvs`, `ip addr show kube-ipvs0` on a node.
  - **Predict:** both ping results.
  - **Verify:** iptables mode: no reply (only the Service's TCP/UDP ports are DNATed). IPVS: replies, because every ClusterIP is bound to the `kube-ipvs0` dummy interface.
- [ ] **14.4 conntrack holds the translation** *(Level: Core)*
  - **Do:** from a Pod on `lab-worker`, open a long connection to a ClusterIP (`nc <ip> 80` and hold). In `nodeshell lab-worker`: `conntrack -L -d <clusterIP>` (§23). Then delete the backend Pod.
  - **Predict:** the reply-direction source address in the conntrack entry; what happens to the held connection.
  - **Verify:** the entry shows the original dst = ClusterIP and the reply src = backend Pod IP; the connection dies or hangs, and a new one lands on another backend.
- [ ] **14.5 Endpoint conditions during a rollout** *(Level: Stretch)*
  - **Do:** `kubectl get endpointslice -l kubernetes.io/service-name=web -o json -w | jq -c '.endpoints[] | [.targetRef.name, .conditions]'` during a rolling update with `preStop: sleep 10` (§5). Also `nft list table ip kube-proxy | head -40` in the `nft` cluster.
  - **Verify:** you observe `ready=false, serving=true, terminating=true` for 10 s per old Pod; the nftables table uses a verdict map instead of a chain per Service.

**Checkpoint (closed book):**
1. What are the `--probability` values for 4 endpoints?
2. Why does a ClusterIP answer ping in IPVS mode and not in iptables mode?
3. In iptables mode, what does the cost of a proxy sync grow with?
<details><summary>Answers</summary>

1. 0.25, 0.333…, 0.5, then an unconditional jump.
2. IPVS binds every ClusterIP to the `kube-ipvs0` dummy interface, so the node owns the address. iptables mode only DNATs matching TCP/UDP/SCTP port traffic; nothing answers ICMP.
3. The total number of rules, roughly Services × endpoints. The rules are rewritten through `iptables-restore` (partial syncs help but the full syncs remain).
</details>

---

## 15 — CNI and Pod Networking  ([chapter](15-cni-and-pod-networking.md))
**Time:** ~4 h · **Needs:** kind `lab`, `calico` cluster, Linux VM for the stretch

- [ ] **15.1 Follow a packet across the veth pair** *(Level: Core)*
  - **Goal:** find a Pod's host-side interface and watch a cross-node packet (§6, Appendix).
  - **Do:** Pod A on `lab-worker`, Pod B on `lab-worker2`. `kubectl exec a -- cat /sys/class/net/eth0/iflink` → N; `docker exec lab-worker ip -o link | grep "^N:"` → the veth. In `nodeshell lab-worker` run `tcpdump -ni <veth> icmp` and `tcpdump -ni eth0 icmp` while A pings B. `ip route` on the node.
  - **Predict:** whether the packet on the node's `eth0` is encapsulated, and its src/dst IPs.
  - **Verify:** kindnet routes natively: the same Pod IPs appear on `eth0`, no outer header; the node route table has `<B's podCIDR> via <worker2 IP>`.
- [ ] **15.2 Call a CNI plugin by hand** *(Level: Core)*
  - **Goal:** use the CNI exec interface without a runtime (§3, §4).
  - **Do:** read `/etc/cni/net.d/*.conflist` and `ls /opt/cni/bin` on a node. In `nodeshell`: `ip netns add t`; write a `bridge` + `host-local` config; `CNI_COMMAND=ADD CNI_CONTAINERID=t1 CNI_NETNS=/var/run/netns/t CNI_IFNAME=eth0 CNI_PATH=/opt/cni/bin /opt/cni/bin/bridge < br.json`. Then `DEL` with the same env.
  - **Predict:** what the plugin prints on stdout, and what `DEL` leaves behind.
  - **Verify:** a JSON result with the allocated IP; `ip netns exec t ip addr` shows it; after `DEL` the veth and the IPAM file under `/var/lib/cni/networks/` are gone.
- [ ] **15.3 VXLAN on the wire** *(Level: Core)*
  - **Goal:** see encapsulation and its overhead (§8, §19).
  - **Do:** in the `calico` cluster with VXLAN encapsulation, ping across nodes while `tcpdump -ni eth0 -vv udp port 4789` runs on a node. `ip -d link show vxlan.calico`.
  - **Predict:** outer and inner IPs, the header overhead, and the MTU on `vxlan.calico`.
  - **Verify:** outer = node IPs, inner = Pod IPs, VNI shown; overhead 50 bytes; interface MTU = underlay MTU − 50.
- [ ] **15.4 The MTU black hole** *(Level: Stretch)*
  - **Goal:** reproduce the "small requests work, big ones hang" outage (§19).
  - **Do:** in the `calico` cluster, set the Pod MTU to the underlay MTU (1500) while VXLAN is on. From a Pod, `ping -M do -s 1472 <remote pod>` and `-s 1400`; `curl` a 1 MB file from a remote Pod.
  - **Predict:** which of the three work.
  - **Verify:** 1400 works; 1472 fails with DF set. Whether the big curl stalls depends on whether ICMP "fragmentation needed" reaches the sender: capture `icmp[0]==3 and icmp[1]==4` on the node and explain your result. Restore the MTU.
- [ ] **15.5 Write a CNI plugin** *(Level: Stretch)*
  - **Do:** a ~100-line plugin in bash or Python for `ADD`/`DEL`/`VERSION`: veth pair, move one end into `$CNI_NETNS`, IP from a file-based pool, return the CNI result JSON (§18). Drive it with `cnitool` or the env-var call from 15.2. This is also chapter 38's phase 9.
  - **Verify:** two namespaces get distinct IPs and can ping each other through your bridge; `DEL` is idempotent (run it twice without error).

**Checkpoint (closed book):**
1. How does the container runtime invoke a CNI plugin?
2. What is the VXLAN overhead, and what does it mean for Pod MTU?
3. How do you find a Pod's host-side veth?
<details><summary>Answers</summary>

1. It execs the plugin binary with `CNI_COMMAND`, `CNI_CONTAINERID`, `CNI_NETNS`, `CNI_IFNAME`, `CNI_PATH` in the environment and the network config JSON on stdin; the result JSON comes back on stdout.
2. 50 bytes (outer Ethernet 14 + IP 20 + UDP 8 + VXLAN 8). The Pod MTU must be at most underlay MTU − 50, for example 1450 on a 1500 network.
3. Read `/sys/class/net/eth0/iflink` inside the Pod; that is the ifindex of the peer on the host (`ip -o link | grep "^<N>:"`).
</details>

---

## 16 — Cilium and eBPF Deep Dive  ([chapter](16-cilium-and-ebpf-deep-dive.md))
**Time:** ~4 h · **Needs:** `cilium` kind cluster, `cilium` and `hubble` CLIs, Linux VM with clang for the stretch

- [ ] **16.1 Services without iptables** *(Level: Core)*
  - **Goal:** find Service routing in BPF maps (§8).
  - **Do:** `docker exec cilium-worker iptables-save | grep -c KUBE-SVC`; create a 3-replica Service; `kubectl -n kube-system exec ds/cilium -- cilium-dbg service list` and `cilium-dbg bpf lb list`.
  - **Predict:** the KUBE-SVC count, and how many LB map entries one ClusterIP:port produces.
  - **Verify:** 0 iptables Service rules; one frontend entry plus one entry per backend for the ClusterIP. Compare with 14.1.
- [ ] **16.2 Where the ClusterIP disappears** *(Level: Core)*
  - **Goal:** observe socket-level load balancing (§8, §9).
  - **Do:** find the client Pod's host interface (`lxc…`, via `iflink`). `tcpdump -ni <lxc> tcp port 80` in `nodeshell cilium-worker` while the Pod curls the ClusterIP.
  - **Predict:** the destination IP in the captured SYN.
  - **Verify:** the backend Pod IP, never the ClusterIP: the address was rewritten at `connect()` by a cgroup hook, before any packet existed.
- [ ] **16.3 A policy drop, seen by Hubble** *(Level: Core)*
  - **Do:** `cilium hubble enable`, `cilium hubble port-forward &`. Apply a `CiliumNetworkPolicy` that allows ingress to `web` only from `role=frontend`. Curl from an unlabelled Pod. `hubble observe --verdict DROPPED --to-label app=web` (§11, §13).
  - **Verify:** a DROPPED flow with reason `Policy denied` and the source and destination security identities; `cilium-dbg endpoint list` shows the same identity numbers.
- [ ] **16.4 Inventory the datapath** *(Level: Core)*
  - **Goal:** see which hooks Cilium uses (§5, §9, §20).
  - **Do:** `kubectl -n kube-system exec ds/cilium -- bpftool net show` and `bpftool prog show | awk '{print $4}' | sort | uniq -c`; `bpftool map show | wc -l`.
  - **Predict:** which program types you will see (tc/sched_cls, cgroup sock_addr, XDP?).
  - **Verify:** a table of program type → count; name which hook implements 16.2 and which implements 16.3.
- [ ] **16.5 Make the verifier say no** *(Level: Stretch)*
  - **Goal:** hit the verifier's termination proof (§3).
  - **Do:** on the Linux VM, an XDP program in C with a loop bounded by a packet byte; compile with `clang -O2 -g -target bpf -c x.c -o x.o`; `ip link set dev lo xdpgeneric obj x.o sec xdp`. Then bound the loop by a constant.
  - **Verify:** the first load fails with the verifier log (unbounded loop / too many instructions); the constant bound loads. `ip link set dev lo xdpgeneric off` to clean up.

**Checkpoint (closed book):**
1. With socket-LB, where is the ClusterIP translated?
2. What is a Cilium security identity, and why does it scale better than per-IP rules?
3. Why does the verifier reject unbounded loops?
<details><summary>Answers</summary>

1. In a cgroup `connect`/`sendmsg` hook, when the socket connects. The packets carry the backend IP from the first byte.
2. A numeric ID derived from the security-relevant labels of a set of endpoints. Policy maps are keyed by identity, so Pod churn does not rewrite rules per IP.
3. It must prove that every program terminates within a bounded instruction count (the complexity limit), so the kernel cannot hang in a hook.
</details>

---

## 17 — Ingress, Gateway API, and Service Mesh  ([chapter](17-ingress-gateway-and-service-mesh.md))
**Time:** ~5 h · **Needs:** kind `lab`, `istioctl`, `fortio` · **First:** [k8s-learn/service-networking-tasks.md](../k8s-learn/service-networking-tasks.md) Level 5

- [ ] **17.1 Envoy's config is the truth** *(Level: Core)*
  - **Goal:** find your HTTPRoute inside Envoy (§5, §8, §9).
  - **Do:** install the Gateway API CRDs, then Istio (`istioctl install --set profile=ambient` also covers 17.2's ambient run) and a Gateway API `Gateway`; an `HTTPRoute` splitting 90/10 between `v1` and `v2`. `istioctl proxy-config routes deploy/<gateway> -o json | jq '..|.weightedClusters? // empty'`. Send 1000 requests.
  - **Predict:** how the weights appear in Envoy, and the observed split.
  - **Verify:** `weighted_clusters` with 90/10; the counted split is within a few percent.
- [ ] **17.2 The mesh latency tax** *(Level: Core)*
  - **Goal:** put numbers on sidecar and ambient overhead (§10, §11, §25).
  - **Do:** `fortio load -qps 1000 -c 16 -t 30s http://echo:8080/` from a client Pod: (a) no mesh, (b) both namespaces `istio-injection=enabled`, (c) ambient mode (`istio.io/dataplane-mode=ambient`).
  - **Predict:** added p50 and p99 per mode.
  - **Verify:** a table of p50/p90/p99 for the three runs, plus the sidecar's CPU from `kubectl top pod --containers`.
- [ ] **17.3 Retry amplification** *(Level: Core)*
  - **Goal:** reproduce the retry storm (§13, §26).
  - **Do:** a backend that always returns 503 and counts requests. Retries `attempts: 3` on the gateway route and on the client sidecar's VirtualService. Send 10 requests.
  - **Predict:** backend hits.
  - **Verify:** up to (1+3)×(1+3) = 16 per client request, 160 total. Remove one layer's retries and measure again.
- [ ] **17.4 mTLS on the wire** *(Level: Core)*
  - **Do:** two meshed Pods; `PeerAuthentication` `STRICT`. `tcpdump -A -ni <veth> port 8080` in `nodeshell` during a request; `istioctl proxy-config secret <pod> -o json` (§12).
  - **Predict:** whether the HTTP path is readable in the capture.
  - **Verify:** plaintext without mesh, a TLS handshake with it; the cert's URI SAN is `spiffe://cluster.local/ns/<ns>/sa/<sa>`.
- [ ] **17.5 The sidecar startup race** *(Level: Stretch)*
  - **Do:** an app container that curls an external URL as its first action and exits non-zero on failure. Run with the sidecar and `holdApplicationUntilProxyStarts: false`, then `true` (§10). Or use the native-sidecar mode of your Istio version.
  - **Verify:** failure count in 10 Pod starts for each setting.

**Checkpoint (closed book):**
1. How does the Gateway API split responsibility across roles?
2. Why are retries at more than one layer dangerous?
3. What identity does Istio put in a workload certificate?
<details><summary>Answers</summary>

1. GatewayClass (infrastructure provider), Gateway (cluster operator: listeners, addresses, TLS), Routes (application teams: matching and backends), with ReferenceGrant for cross-namespace references.
2. They multiply: attempts at each layer compound, so a failing backend receives many times the offered load exactly when it is weakest.
3. A SPIFFE ID derived from the namespace and ServiceAccount: `spiffe://<trust-domain>/ns/<ns>/sa/<sa>`.
</details>

---

## 18 — DNS and CoreDNS  ([chapter](18-dns-and-coredns.md))
**Time:** ~3 h · **Needs:** kind `lab` · **First:** [k8s-learn/service-networking-tasks.md](../k8s-learn/service-networking-tasks.md) Level 3

- [ ] **18.1 Queries per lookup, counted** *(Level: Core)*
  - **Goal:** measure the ndots tax instead of describing it (§2, §3).
  - **Do:** `kubectl -n kube-system port-forward deploy/coredns 9153 &`. Sum `coredns_dns_requests_total` before and after 100 × `getent ahosts example.com` in a Debian Pod. Repeat with `example.com.` (trailing dot) and with `dnsConfig.options: [{name: ndots, value: "1"}]`.
  - **Predict:** queries per lookup in all three cases, from the Pod's `/etc/resolv.conf`.
  - **Verify:** a table: variant → queries per lookup. The default case is (search domains + 1) × 2 (A and AAAA).
- [ ] **18.2 The latency cost** *(Level: Core)*
  - **Do:** time 200 lookups for each variant from 18.1. Add `log` to the Corefile (`kubectl -n kube-system edit cm coredns`) and read one default lookup's NXDOMAIN sequence in the CoreDNS logs.
  - **Predict:** the ratio of the default to the trailing-dot time.
  - **Verify:** both times and the ratio; the log shows each search-suffixed query returning NXDOMAIN before the absolute one.
- [ ] **18.3 Negative caching** *(Level: Core)*
  - **Goal:** see the window when a new Service does not resolve (§12).
  - **Do:** in a loop, look up `late.default.svc.cluster.local` once per second (`getent hosts`). After 5 NXDOMAINs, create Service `late`. Record how long the NXDOMAIN persists.
  - **Predict:** the maximum persistence, from the `cache` plugin's settings in your Corefile.
  - **Verify:** measured persistence is at most the denial TTL; set `cache 5` and repeat.
- [ ] **18.4 Kill CoreDNS** *(Level: Core, destructive)*
  - **Do:** `kubectl -n kube-system scale deploy coredns --replicas=0`. Test an existing HTTP keep-alive connection, a new lookup of a Service name, and a lookup of `example.com`.
  - **Predict:** which of the three still work.
  - **Verify:** the existing connection works (no DNS involved); new lookups fail with timeouts after the resolver's retries. Record the timeout duration (`options timeout:` and `attempts:`). Scale back up.
- [ ] **18.5 autopath** *(Level: Stretch)*
  - **Do:** enable `autopath @kubernetes` with `pods verified` in the Corefile (§12) and repeat 18.1.
  - **Verify:** queries per lookup drop to ~2 for external names; CoreDNS memory (`kubectl top pod`) rises. One sentence on the trade.

**Checkpoint (closed book):**
1. With `ndots:5` and 3 search domains, how many queries does a lookup of `api.example.com` cost?
2. Name three fixes for the ndots tax.
3. How does CoreDNS know a Service's ClusterIP?
<details><summary>Answers</summary>

1. 8: three search-suffixed names × (A + AAAA) all NXDOMAIN, then the absolute name × 2. More if the node adds search domains.
2. Trailing dots on external FQDNs; a lower `ndots` via `dnsConfig`; NodeLocal DNSCache or `autopath` to cut round trips (and caching resolvers in the app).
3. The `kubernetes` plugin watches Services and EndpointSlices through the apiserver and answers from its in-memory cache.
</details>

---

## 19 — Storage: CSI, PV, PVC  ([chapter](19-storage-csi-pv-pvc.md))
**Time:** ~4 h · **Needs:** kind `lab`, `csi-driver-host-path` · **First:** [k8s-learn/config-storage-tasks.md](../k8s-learn/config-storage-tasks.md) Levels 4–5

- [ ] **19.1 kind's default storage is not CSI** *(Level: Core)*
  - **Do:** `kubectl get sc standard -o jsonpath='{.provisioner}'`; `kubectl get csidrivers`. Bind a PVC and read the PV's `.spec` (§2, §16).
  - **Predict:** the provisioner name and the PV's volume source.
  - **Verify:** `rancher.io/local-path`, no CSIDriver objects; the PV is a `hostPath`/`local` volume with `nodeAffinity` to one node. Find the directory on that node.
- [ ] **19.2 Trace the three phases** *(Level: Core)*
  - **Goal:** see each CSI RPC and who calls it (§4–§8).
  - **Do:** install the external-snapshotter CRDs and controller, then `csi-driver-host-path` (`deploy/kubernetes-latest/deploy.sh`) and its example StorageClass. Create a PVC and a Pod. Grep the plugin's and sidecars' logs for `CreateVolume`, `ControllerPublishVolume`, `NodeStageVolume`, `NodePublishVolume`. `kubectl get volumeattachments`.
  - **Predict:** the order of calls and whether a VolumeAttachment will exist.
  - **Verify:** a timestamped list of RPCs, each labelled with its caller (external-provisioner, external-attacher, kubelet).
- [ ] **19.3 Find the mount** *(Level: Core)*
  - **Do:** on the node, `findmnt | grep <pv-name>`.
  - **Predict:** the path shape.
  - **Verify:** a mount under `/var/lib/kubelet/pods/<pod-uid>/volumes/kubernetes.io~csi/<pv>/mount` (and a staging path if the driver stages). Delete the Pod and confirm the mount is gone.
- [ ] **19.4 PVC protection and reclaim** *(Level: Core)*
  - **Do:** delete a PVC that a running Pod uses; read `.metadata.finalizers`. Delete the Pod. Watch the PV with `reclaimPolicy: Delete` (§23, §24).
  - **Predict:** the PVC's state before and after the Pod goes, and the PV's fate.
  - **Verify:** PVC `Terminating` with `kubernetes.io/pvc-protection` until the Pod is gone; then PVC and PV are deleted, and `DeleteVolume` appears in the driver log.
- [ ] **19.5 Snapshot and restore** *(Level: Core)*
  - **Do:** write a file to a hostpath-CSI PVC; create a `VolumeSnapshotClass` and `VolumeSnapshot`; create a new PVC with `dataSource` pointing at the snapshot (§11).
  - **Verify:** `readyToUse: true`; the restored volume contains the file; `CreateSnapshot` and `CreateVolume` (with a content source) appear in the logs.
- [ ] **19.6 IOPS of three volume types** *(Level: Stretch)*
  - **Do:** `fio --name=r --rw=randread --bs=4k --size=512m --runtime=30 --time_based --direct=1` on `emptyDir`, a local-path PVC, and a hostpath-CSI PVC (§19).
  - **Verify:** three IOPS numbers; explain why they are nearly equal here and would not be on a cloud disk.

**Checkpoint (closed book):**
1. Name the three CSI phases and who calls each.
2. Why does `WaitForFirstConsumer` exist?
3. What keeps a PVC in `Terminating` while a Pod uses it?
<details><summary>Answers</summary>

1. Provision (`CreateVolume`, by external-provisioner), attach (`ControllerPublishVolume`, by external-attacher through a VolumeAttachment from the attach/detach controller), mount (`NodeStageVolume`/`NodePublishVolume`, by the kubelet on the node).
2. So the volume is provisioned in the topology (zone or node) where the scheduler places the Pod, instead of pinning the Pod to wherever the volume was created.
3. The `kubernetes.io/pvc-protection` finalizer, removed once no Pod uses the claim.
</details>

---

## 20 — Network Policy and Segmentation  ([chapter](20-network-policy-and-segmentation.md))
**Time:** ~3 h · **Needs:** `calico` or `cilium` cluster (a CNI that enforces policy)

- [ ] **20.1 Is anything enforcing your policy?** *(Level: Core)*
  - **Goal:** see that the API accepts policies nobody enforces (§8).
  - **Do:** apply the same default-deny-ingress policy to a namespace in `lab` and in `calico`; curl a Pod in it from another namespace in each cluster.
  - **Predict:** both results.
  - **Verify:** `calico` blocks; in `lab` the result depends on whether your kindnet version implements NetworkPolicy. Write down which, and the one command that would have told you in advance.
- [ ] **20.2 Default-deny egress breaks DNS** *(Level: Core)*
  - **Goal:** reproduce the most common policy outage (§5, §7).
  - **Do:** in namespace `app`, a default-deny egress policy plus an allow to `app=db` on TCP 5432. From a client Pod: `nc -zv db 5432` and `nc -zv <db-pod-ip> 5432`.
  - **Predict:** both results.
  - **Verify:** by name fails (the lookup times out), by IP works. Fix with an egress rule to `namespaceSelector: {kubernetes.io/metadata.name: kube-system}` + `podSelector: {k8s-app: kube-dns}` on UDP **and** TCP 53; both succeed.
- [ ] **20.3 One element or two: AND vs OR** *(Level: Core)*
  - **Goal:** feel the selector trap of §4.
  - **Do:** four client Pods: {ns labelled `team=a` or not} × {Pod labelled `role=client` or not}. Policy P1 has one `from` element with both `namespaceSelector` and `podSelector`; P2 has them as two elements. Write a script that curls the server from all four with a 1 s timeout and prints a 2×2 matrix.
  - **Predict:** the matrix for P1 and for P2.
  - **Verify:** P1 allows only the (team=a, role=client) cell (AND); P2 allows three cells (OR).
- [ ] **20.4 hostNetwork walks past** *(Level: Core)*
  - **Do:** keep a default-deny ingress on `web`. Curl it from a `hostNetwork: true` Pod on the same node and on another node (§17).
  - **Predict:** both results.
  - **Verify:** record what your CNI does with host-sourced traffic (Calico and Cilium treat the local host specially). One sentence on why `hostNetwork` must be blocked by admission policy, not NetworkPolicy.
- [ ] **20.5 AdminNetworkPolicy beats the namespace** *(Level: Stretch)*
  - **Do:** on a CNI that implements ANP (check its docs), an `AdminNetworkPolicy` that denies egress from all tenant namespaces to `169.254.169.254/32`, and a namespace NetworkPolicy that allows all egress. Then change the ANP action to `Pass` (§13, §14).
  - **Verify:** `Deny` wins over the namespace allow; with `Pass` the namespace policy decides.

**Checkpoint (closed book):**
1. A Pod is selected by no NetworkPolicy. What traffic does it accept?
2. What is the difference between `from: [{namespaceSelector: X, podSelector: Y}]` and `from: [{namespaceSelector: X}, {podSelector: Y}]`?
3. What is the minimal egress allowance that keeps DNS working under default-deny?
<details><summary>Answers</summary>

1. All of it. A Pod is isolated for a direction only once some policy selects it for that direction.
2. The first is one peer that must match both (AND). The second is two peers, either of which matches (OR), and the bare `podSelector` means "in the policy's own namespace".
3. Egress to the kube-dns Pods in `kube-system` on UDP and TCP port 53 (or to NodeLocal DNSCache's address if you run it).
</details>

---

## 21 — Resource Management and QoS  ([chapter](21-resource-management-and-qos.md))
**Time:** ~4 h · **Needs:** kind `lab`, a second kind cluster with a KubeletConfiguration patch · **First:** [k8s-learn/resources-tasks.md](../k8s-learn/resources-tasks.md) Levels 1–4

- [ ] **21.1 CPU throttling via `cpu.stat`** *(Level: Core)*
  - **Goal:** measure throttling from the cgroup itself, not a dashboard (§9, §27).
  - **Do:** a Pod with `limits.cpu: 200m` running one busy thread. `kubectl exec` → `cat /sys/fs/cgroup/cpu.stat` and `cat /sys/fs/cgroup/cpu.max`, twice, 10 s apart.
  - **Predict:** `cpu.max`, and the deltas of `nr_periods`, `nr_throttled`, `throttled_usec` over 10 s.
  - **Verify:** `20000 100000`; ≈100 periods, ≈100 throttled, `throttled_usec` ≈ 8,000,000.
- [ ] **21.2 Throttling at 25% average use** *(Level: Core)*
  - **Goal:** reproduce the latency tail of §10 and §27.
  - **Do:** `limits.cpu: 1`, 4 worker threads that each wake every 100 ms and burn 25 ms (a 20-line Python script with `multiprocessing`). A 5th process times a 2 ms unit of work in a loop and records p50/p99.
  - **Predict:** p99 of the 2 ms unit, and average CPU use.
  - **Verify:** average ≈ 1 CPU or less, yet `nr_throttled` rises and p99 is tens of ms. Remove the limit and record p99 again.
- [ ] **21.3 Read the cgroup files Kubernetes wrote** *(Level: Core)*
  - **Goal:** map requests and limits to kernel knobs per QoS class (§4–§6, §23).
  - **Do:** one Guaranteed (1 CPU / 256Mi), one Burstable (250m request, no limits), one BestEffort Pod. In `nodeshell`, for each: `cat /proc/<pid>/cgroup`, then `cpu.weight`, `cpu.max`, `memory.max` in that directory, and `cat /proc/<pid>/oom_score_adj`.
  - **Predict:** the cgroup path shape per class, `cpu.weight` for the 1-CPU request, and the three `oom_score_adj` values (Burstable from §8's formula and the node's memory capacity).
  - **Verify:** Guaranteed sits directly under the kubepods slice, the others under `burstable`/`besteffort`; `cpu.weight` = 39 for 1 CPU with the classic linear shares-to-weight conversion (newer runc versions map 1024 shares to 100; note which yours uses); `oom_score_adj` −997 / computed / 1000.
- [ ] **21.4 Static CPU manager pins cores** *(Level: Core)*
  - **Goal:** see exclusive cores (§11).
  - **Do:** a kind cluster with a top-level patch `kind: KubeletConfiguration` + `cpuManagerPolicy: static` + `reservedSystemCPUs: "0"`. A Guaranteed Pod with `cpu: 2`, and a Burstable Pod. Read `cat /sys/fs/cgroup/cpuset.cpus.effective` in each, and `/var/lib/kubelet/cpu_manager_state` on the node.
  - **Predict:** each Pod's CPU set.
  - **Verify:** the Guaranteed Pod gets 2 exclusive CPUs that disappear from the Burstable Pod's set; the state file lists the assignment.
- [ ] **21.5 PID limits** *(Level: Stretch)*
  - **Do:** in the same cluster add `podPidsLimit: 100`. In a Pod: `for i in $(seq 200); do sleep 600 & done` (§21).
  - **Predict:** how many `sleep`s start.
  - **Verify:** fork fails near 100 (minus the shell); `pids.max` = 100 and `pids.events` shows `max` > 0. The node is unaffected.

**Checkpoint (closed book):**
1. Which cgroup v2 file does each of CPU request, CPU limit, and memory limit become?
2. Why can a container at 30% average CPU be heavily throttled?
3. What `oom_score_adj` does each QoS class get?
<details><summary>Answers</summary>

1. CPU request → `cpu.weight`; CPU limit → `cpu.max` (quota and period); memory limit → `memory.max`. A memory request writes nothing unless MemoryQoS is on (then `memory.min`/`memory.high`).
2. The quota is per 100 ms period and shared by all threads. Several threads bursting together use it up early in the period and then wait for the rest of it.
3. Guaranteed −997, BestEffort 1000, Burstable `1000 − 1000 × memoryRequest / nodeCapacity`, clamped to [2, 999].
</details>

---

## 22 — Autoscaling  ([chapter](22-autoscaling.md))
**Time:** ~5 h · **Needs:** kind `lab`, metrics-server, kube-prometheus-stack + prometheus-adapter, KEDA, `hey` · **First:** [k8s-learn/deployment-tasks.md](../k8s-learn/deployment-tasks.md) Task 4.3

- [ ] **22.1 Predict the HPA's arithmetic** *(Level: Core)*
  - **Goal:** use the formula of §4 before the controller does.
  - **Do:** an HPA with CPU `averageUtilization: 50`, 2 replicas. Drive load until `kubectl get hpa` shows a steady current value. Then tune the load so the ratio is ~1.05.
  - **Predict:** desired replicas at the first steady value; whether the 1.05 ratio triggers a change.
  - **Verify:** `ceil(currentReplicas × current / target)` matches the next scale event in `kubectl describe hpa`; at 1.05 nothing happens (10% tolerance).
- [ ] **22.2 An HPA fed by a custom metric** *(Level: Core)*
  - **Goal:** wire the custom metrics pipeline end to end (§6, §8).
  - **Do:** a small Python app exposing `http_requests_total` with `prometheus_client`; a ServiceMonitor; a prometheus-adapter rule that turns it into `http_requests_per_second` (a `rate(...[1m])`). Check `kubectl get --raw "/apis/custom.metrics.k8s.io/v1beta1/namespaces/default/pods/*/http_requests_per_second" | jq`. An HPA of type `Pods` with `averageValue: "10"`. `hey -z 5m -q 50 -c 1` against it.
  - **Predict:** the steady replica count at 50 rps.
  - **Verify:** 5 replicas; you can name every hop (app → Prometheus → adapter → APIService → HPA) and one command that checks each.
- [ ] **22.3 The scale-down staircase** *(Level: Core)*
  - **Goal:** see stabilization and policies (§10).
  - **Do:** stop the load from 22.2 and record replicas every 10 s. Then set `behavior.scaleDown: {stabilizationWindowSeconds: 30, policies: [{type: Pods, value: 1, periodSeconds: 15}]}` and repeat.
  - **Predict:** time until the first scale-down in both runs, and the shape of the second.
  - **Verify:** ≈300 s default window; then ≈30 s followed by one Pod per 15 s.
- [ ] **22.4 KEDA from zero** *(Level: Core)*
  - **Goal:** see who handles 0 ↔ 1 (§25, §28).
  - **Do:** install KEDA; a `ScaledObject` with `minReplicaCount: 0` and a Prometheus trigger on the metric from 22.2. `kubectl get hpa` after creation.
  - **Predict:** what the KEDA-created HPA's `minReplicas` is, and who scales 0 → 1.
  - **Verify:** the HPA has `minReplicas: 1`; KEDA's operator scales the Deployment 0 → 1 on activation and back to 0 after `cooldownPeriod`. Time the first request's latency from zero.
- [ ] **22.5 VPA's opinion vs yours** *(Level: Stretch)*
  - **Do:** install VPA; `updateMode: "Off"` on a Deployment with a known load for 15 minutes. Compare `status.recommendation` with your own right-sizing from `k8s-learn/resources-tasks.md` Task 6.1 (§13, §14).
  - **Verify:** target, lower and upper bounds written down next to your number; explain the gap with the recommender's percentile and margin.

**Checkpoint (closed book):**
1. Write the HPA formula and the default tolerance.
2. An HPA has two metrics that disagree. Which wins?
3. Why can't a plain HPA scale a Deployment to zero?
<details><summary>Answers</summary>

1. `desired = ceil(current × currentMetric / targetMetric)`; no change when the ratio is within 10% of 1.0.
2. The one that asks for the most replicas (the max over metrics).
3. `minReplicas` must be ≥ 1 unless the alpha `HPAScaleToZero` gate is on, and with zero Pods there is no per-Pod metric to compute from. KEDA uses an external signal to go 0 ↔ 1 and hands 1 ↔ N to an HPA.
</details>

---

## 23 — CRDs, Operators, and controller-runtime  ([chapter](23-crds-operators-and-controller-runtime.md))
**Time:** ~5 h · **Needs:** kind `lab`, Go + kubebuilder · **First:** [k8s-learn/operator-tasks.md](../k8s-learn/operator-tasks.md) Levels 1–4, [api-machinery-tasks.md](../k8s-learn/api-machinery-tasks.md) Level 6

- [ ] **23.1 Pruning by the structural schema** *(Level: Core)*
  - **Goal:** see unknown fields dropped (§3, §5).
  - **Do:** a CRD whose schema has only `spec.size`. `kubectl apply --validate=false` a CR with `spec.size: 3` and `spec.extra: 1`; read it back. Then add `x-kubernetes-preserve-unknown-fields: true` under `spec` and repeat. Finally apply with the default `--validate=strict`.
  - **Predict:** the stored object in each case.
  - **Verify:** `extra` is pruned, then kept; strict validation rejects the apply with `unknown field`.
- [ ] **23.2 How a CR sits in etcd** *(Level: Core)*
  - **Do:** `ketcd get /registry/<group>/<plural>/default/<name> --print-value-only | head -c 200` (§12).
  - **Predict:** the encoding and the `apiVersion` in the stored bytes.
  - **Verify:** plain JSON (compare 05.3's protobuf Pod) with the storage version's `apiVersion`.
- [ ] **23.3 Two versions and a conversion webhook** *(Level: Core)*
  - **Goal:** make conversion real (§8–§10).
  - **Do:** `v1alpha1` with `spec.size` (string) and `v1` with `spec.replicas` (int); `v1` is the storage version; a conversion webhook (kubebuilder `Hub`/`Convertible`, or 40 lines of Python). Create through `v1alpha1`, read through both, and read etcd.
  - **Predict:** what etcd holds, and `status.storedVersions`.
  - **Verify:** etcd has `v1` JSON; `storedVersions: ["v1"]`. Flip storage to `v1alpha1`, write one object, and see `storedVersions` grow to both. Write the migration steps you need before you can remove a version.
- [ ] **23.4 The scale subresource** *(Level: Core)*
  - **Do:** enable `subresources.scale` with `specReplicasPath`, `statusReplicasPath`, `labelSelectorPath` (§6). `kubectl scale` your CR; attach an HPA to it.
  - **Predict:** whether the HPA works without your controller knowing anything about HPAs.
  - **Verify:** `kubectl get --raw /apis/<g>/<v>/namespaces/default/<plural>/<name>/scale` returns a `Scale` object; the HPA changes `spec.replicas` on your CR.
- [ ] **23.5 Operator throughput** *(Level: Stretch)*
  - **Do:** create 2000 CRs; reconcile each with a 50 ms simulated call. Measure time to drain with `MaxConcurrentReconciles` 1, 4 and 16, from `controller_runtime_reconcile_total` and `workqueue_depth`. Record the manager's RSS (§27).
  - **Verify:** a table concurrency → drain time → RSS; explain where it stops scaling (client-side QPS limits, your simulated call, or the apiserver).

**Checkpoint (closed book):**
1. What is a structural schema, and what happens to fields it does not describe?
2. How is a CR stored in etcd compared with a built-in type?
3. What does `status.storedVersions` record, and why does it block removing a version?
<details><summary>Answers</summary>

1. A schema where every field has a type and there are no ambiguous constructs. Unspecified fields are pruned on write unless `x-kubernetes-preserve-unknown-fields` is set.
2. As JSON in its storage version; built-ins are protobuf.
3. Every version ever used as the storage version. Objects may still be stored in an old version, so you must rewrite them (storage migration) and then trim the list before removing that version.
</details>

---

## 24 — API Aggregation and Extension API Servers  ([chapter](24-api-aggregation-and-extension-apiservers.md))
**Time:** ~4 h · **Needs:** kind `lab`, metrics-server, Python for the echo server

- [ ] **24.1 Follow the metrics API to its source** *(Level: Core)*
  - **Do:** `kubectl get apiservices | grep -v Local`; `kubectl get apiservice v1beta1.metrics.k8s.io -o yaml`; `kubectl get --raw /apis/metrics.k8s.io/v1beta1/nodes | jq '.items[0].usage'` and `kubeletmetrics lab-worker /resource | grep node_cpu` (§3, §12).
  - **Predict:** which Service backs the APIService and where metrics-server gets its numbers.
  - **Verify:** the Service in `kube-system`; the node CPU value matches the kubelet's `/metrics/resource` sample within one scrape interval.
- [ ] **24.2 Break the aggregated API** *(Level: Core, destructive)*
  - **Goal:** see an unavailable APIService's blast radius (§18, §25).
  - **Do:** `kubectl -n kube-system scale deploy metrics-server --replicas=0`. Then: `kubectl api-resources`, `kubectl top pods`, create and delete a namespace containing one ConfigMap, and check an HPA.
  - **Predict:** the result of each.
  - **Verify:** `api-resources` warns about `metrics.k8s.io/v1beta1`; `top` fails; the namespace stays `Terminating` with a `NamespaceDeletionDiscoveryFailure` condition; the HPA reports `FailedGetResourceMetric`. Scale back and watch each recover.
- [ ] **24.3 An echo extension apiserver** *(Level: Core)*
  - **Goal:** see the aggregator's identity headers (§4, §5, §7).
  - **Do:** a Python HTTPS server that logs all request headers and the client certificate subject, answers discovery `GET /apis/echo.lab.io/v1alpha1` with an `APIResourceList` containing `echoes`, and returns a JSON object for `GET .../echoes`. Deploy it behind a Service and register `v1alpha1.echo.lab.io` with its `caBundle`. Run `kubectl get echoes --as=alice`.
  - **Predict:** which headers carry the user, and which client cert the aggregator presents.
  - **Verify:** `X-Remote-User: alice`, `X-Remote-Group: system:authenticated`, a front-proxy client cert. Read `kube-system/extension-apiserver-authentication` and write the check your server must do before trusting those headers.
- [ ] **24.4 The real library** *(Level: Stretch)*
  - **Do:** build and run `k8s.io/sample-apiserver` with its own etcd; create a `Flunder` (§8–§11).
  - **Verify:** `ketcd get /registry --prefix --keys-only | grep flunder` finds nothing in the main etcd; the object is in the sample server's etcd.

**Checkpoint (closed book):**
1. How does an extension apiserver learn who the user is?
2. Why can one unavailable APIService block namespace deletion?
3. Give two reasons to choose aggregation over a CRD.
<details><summary>Answers</summary>

1. The aggregator authenticates the user, then proxies with `X-Remote-User`/`X-Remote-Group`/`X-Remote-Extra-*` headers over mTLS with the front-proxy client cert. The extension server trusts those headers only from a client cert signed by the request-header CA, and delegates authorization back with SubjectAccessReview.
2. The namespace controller must discover every namespaced resource type to delete its contents. If discovery for one group fails, it cannot prove the namespace is empty, so it stops.
3. Data not stored in etcd or computed on read (metrics); custom storage or very high write rates; non-CRUD verbs or subresources; protobuf or custom validation beyond what CRDs offer.
</details>

---

## 25 — Multi-Tenancy  ([chapter](25-multi-tenancy.md))
**Time:** ~4 h · **Needs:** `calico` cluster (for the network layer), `vcluster` CLI

- [ ] **25.1 A tenant bundle and six breakout attempts** *(Level: Core)*
  - **Goal:** test the five soft-tenancy layers (§5–§10, §12).
  - **Do:** one YAML per tenant: Namespace with `pod-security.kubernetes.io/enforce: restricted`, a RoleBinding of `edit` to group `tenant-a`, ResourceQuota, LimitRange, default-deny NetworkPolicy plus DNS allow. As `--as=u --as-group=tenant-a`: (1) list Pods in `tenant-b`; (2) create a privileged Pod; (3) create a `hostPath` Pod; (4) exceed the quota; (5) create a ClusterRole; (6) curl a Pod in `tenant-b`.
  - **Predict:** each result and which layer stops it.
  - **Verify:** a six-row table: attempt → blocked? → by which layer → error text.
- [ ] **25.2 What still leaks** *(Level: Core)*
  - **Goal:** list what a namespace does not isolate (§3, §4).
  - **Do:** as the tenant, `kubectl auth can-i --list --as=u --as-group=tenant-a --as-group=system:authenticated`; `kubectl get --raw /apis | jq '.groups[].name'`; resolve `<svc>.tenant-b.svc.cluster.local` from a tenant-a Pod.
  - **Predict:** what the tenant can see about other tenants and the cluster.
  - **Verify:** list the leaks you found (API groups and CRDs, other tenants' Service names by DNS, node names from Pod status) and one mitigation for each.
- [ ] **25.3 vCluster's syncer** *(Level: Core)*
  - **Goal:** see object translation (§16–§18).
  - **Do:** `vcluster create t1 -n host-t1`; inside it, create a Deployment, a CRD, and a Namespace. On the host: `kubectl get pods,svc -n host-t1` and `kubectl get crd | grep <yours>`.
  - **Predict:** which objects appear on the host and under what names.
  - **Verify:** Pods and Services appear with translated names (`<name>-x-<ns>-x-t1`); Deployments, Namespaces and the CRD exist only inside the virtual cluster.
- [ ] **25.4 Noisy neighbor at the apiserver** *(Level: Stretch)*
  - **Do:** tenant a floods LISTs (as in 05.4). Measure tenant b's `kubectl get pods` latency, then add a per-tenant FlowSchema with `distinguisherMethod: ByNamespace` or `ByUser` and repeat (§27).
  - **Verify:** b's p95 before and after.

**Checkpoint (closed book):**
1. Name the five soft-tenancy layers.
2. What does vCluster sync to the host cluster?
3. Give three reasons a namespace is not a security boundary.
<details><summary>Answers</summary>

1. Namespaces and naming; RBAC bound to groups; capacity (ResourceQuota, LimitRange, PriorityClass); default-deny networking; workload policy (Pod Security Admission, VAP/Kyverno).
2. Low-level runtime objects (Pods, Services, the ConfigMaps/Secrets/PVCs they need), with translated names. Higher-level objects (Deployments, CRDs, Namespaces) stay in the virtual control plane.
3. Shared kernel and nodes; cluster-scoped resources and API discovery are global; the Pod network and DNS are flat unless policy says otherwise.
</details>

---

## 26 — Multi-Cluster and Fleet  ([chapter](26-multi-cluster-and-fleet.md))
**Time:** ~6 h · **Needs:** 2–3 kind clusters, `clusterctl`, Argo CD, `cilium` CLI for the stretch

- [ ] **26.1 One Git repo, two clusters** *(Level: Core)*
  - **Goal:** model 1 of §8 (GitOps per cluster) with an ApplicationSet (§9).
  - **Do:** kind clusters `c1` (Argo CD) and `c2`. Register `c2` using `kind get kubeconfig --internal --name c2` (the address must be reachable from inside `c1`). An `ApplicationSet` with the cluster generator deploys the same app to both. Then `kubectl --context kind-c2 delete deploy <app>`.
  - **Predict:** time until the Deployment is back on `c2`.
  - **Verify:** both clusters run the app; the deleted Deployment returns (record the delay); the Application shows the event.
- [ ] **26.2 Cluster API with the Docker provider** *(Level: Core)*
  - **Goal:** provision clusters with a controller (§3–§5).
  - **Do:** a management kind cluster with `/var/run/docker.sock` mounted into its node; `export CLUSTER_TOPOLOGY=true`; `clusterctl init --infrastructure docker`; `clusterctl generate cluster w1 --flavor development --kubernetes-version <v> --control-plane-machine-count=1 --worker-machine-count=1 | kubectl apply -f -`. Install a CNI in `w1`. Scale the `MachineDeployment` to 3, then `docker rm -f` one worker container.
  - **Predict:** what CAPI does after the `docker rm`, with and without a `MachineHealthCheck`.
  - **Verify:** `kubectl get cluster,machines,dockermachines` over time; with an MHC, a new Machine replaces the dead one. Time it.
- [ ] **26.3 Explain it** *(Level: Core)*
  - **Do:** a design note (≤ 10 lines) to a platform lead: which of the four propagation models (§8) for 3 regional clusters with asymmetric replica counts, and why kubefed's lessons (§14) rule one of them out.
- [ ] **26.4 A global Service across clusters** *(Level: Stretch)*
  - **Do:** two `cilium` clusters with distinct Pod CIDRs and cluster IDs; `cilium clustermesh enable` and `cilium clustermesh connect`. A Service with `service.cilium.io/global: "true"` in both (§17).
  - **Verify:** 100 curls from `c1` hit backends in both clusters; scale `c1`'s backends to 0 and all traffic fails over.

**Checkpoint (closed book):**
1. How does Cluster API solve the bootstrap chicken-and-egg problem?
2. Name the four workload propagation models.
3. Give two reasons kubefed failed.
<details><summary>Answers</summary>

1. A temporary management cluster (often kind) creates the first real cluster, then `clusterctl move` transfers the CAPI objects into it so it manages itself.
2. GitOps per cluster (Argo CD ApplicationSet, Flux), fleet-native (Rancher Fleet), federation-style (Karmada), and API-surface (KCP, the deprecated kubefed).
3. Type explosion (a `Federated<Type>` CRD for every kind, always behind the ecosystem), and a central control plane that fought local changes and added a single point of failure. See §14.2 for the full list.
</details>

---

## 27 — Supply Chain Security  ([chapter](27-supply-chain-security.md))
**Time:** ~4 h · **Needs:** Docker, `ttl.sh`, `cosign`, `syft`, `trivy`, `crane`, kind `lab` with Kyverno

- [ ] **27.1 Sign by digest, find the signature** *(Level: Core)*
  - **Goal:** see what is signed and where it lives (§5, §6).
  - **Do:** push `ttl.sh/$(uuidgen | tr A-Z a-z):2h`; `crane digest` it. `cosign generate-key-pair`; `cosign sign --key cosign.key --tlog-upload=false <image>@<digest>`; `cosign verify --key cosign.pub --insecure-ignore-tlog=true <image>`; `crane ls <repo>`. (Flag names differ between cosign 2.x and 3.x; check `cosign sign --help`.)
  - **Predict:** the tag name that holds the signature.
  - **Verify:** a `sha256-<digest>.sig` tag (or an OCI referrer, depending on the cosign version); verification prints the signed payload with the manifest digest.
- [ ] **27.2 SBOM, attested** *(Level: Core)*
  - **Do:** `syft <image> -o spdx-json > sbom.json`; count packages with `jq '.packages | length'`. `cosign attest --key cosign.key --type spdxjson --predicate sbom.json --tlog-upload=false <image>@<digest>`; `cosign verify-attestation --key cosign.pub --type spdxjson --insecure-ignore-tlog=true <image> | jq -r .payload | base64 -d | jq '.predicateType, .subject'` (§9–§12).
  - **Verify:** the subject digest equals the image digest; the predicate is your SBOM.
- [ ] **27.3 Enforce signatures at admission** *(Level: Core)*
  - **Goal:** block unsigned images (§21, §23, §24).
  - **Do:** install Kyverno. A `ClusterPolicy` with `verifyImages`, `imageReferences: ["ttl.sh/*"]`, your public key, `mutateDigest: true`, and (for keys without a transparency log) `rekor: {ignoreTlog: true}` and `ctlog: {ignoreSCT: true}`. Deploy the signed image by tag, then an unsigned one.
  - **Predict:** both outcomes and the image string in the admitted Pod.
  - **Verify:** signed: admitted, and the Pod spec now says `@sha256:…`; unsigned: denied with the policy message.
- [ ] **27.4 Retarget the tag** *(Level: Core)*
  - **Do:** push a different, unsigned image to the same tag as 27.3's signed one. Roll the Deployment.
  - **Predict:** what admission does.
  - **Verify:** denied: the tag now resolves to an unsigned digest. One sentence on why a signature on a tag would have been worthless.
- [ ] **27.5 Base image CVE budget** *(Level: Stretch)*
  - **Do:** `trivy image --severity HIGH,CRITICAL -q` on `python:3.12`, `python:3.12-slim`, and a distroless or Chainguard Python image (§25, §27). Also measure the admission latency Kyverno adds: time 20 Pod creates with and without the policy (§33).
  - **Verify:** a table image → size → HIGH+CRITICAL count; the per-create admission cost in ms.

**Checkpoint (closed book):**
1. Does cosign sign a tag or a digest, and where does the signature go?
2. What is inside an in-toto attestation?
3. In keyless signing, what do Fulcio and Rekor each provide?
<details><summary>Answers</summary>

1. The manifest digest. The signature is stored in the registry next to it: a `sha256-<digest>.sig` tag, or an OCI referrer.
2. A statement with `subject` (artifact names and digests), `predicateType`, and `predicate` (for example an SBOM or SLSA provenance), wrapped in a signed DSSE envelope.
3. Fulcio issues a short-lived certificate binding the signer's OIDC identity to an ephemeral key. Rekor is the transparency log that records the signature and proves it was made while the certificate was valid.
</details>

---

## 28 — Runtime Security and Policy  ([chapter](28-runtime-security-and-policy.md))
**Time:** ~4 h · **Needs:** kind `lab` on a Linux host kernel with BTF (for Falco's modern eBPF driver), `helm`

- [ ] **28.1 PSA dry run on a live namespace** *(Level: Core)*
  - **Do:** `kubectl label --dry-run=server --overwrite ns kube-system pod-security.kubernetes.io/enforce=restricted` (§4, §5).
  - **Predict:** how many existing Pods would violate `restricted`.
  - **Verify:** the warning list; group the violations by rule (`runAsNonRoot`, `seccompProfile`, `capabilities`, `hostPath`, ...).
- [ ] **28.2 Three modes at once** *(Level: Core)*
  - **Do:** a namespace with `enforce=baseline`, `warn=restricted`, `audit=restricted`. Create a root Pod with no seccomp profile, then a `privileged: true` Pod.
  - **Predict:** each outcome, and where each mode's output appears.
  - **Verify:** the root Pod is admitted with a kubectl warning, and its audit event carries `pod-security.kubernetes.io/audit-violations`; the privileged Pod is rejected.
- [ ] **28.3 What seccomp is a Pod really running?** *(Level: Core)*
  - **Goal:** check the default and write a Localhost profile (§22).
  - **Do:** `grep Seccomp /proc/self/status` in a Pod with no `securityContext` and in one with `seccompProfile: {type: RuntimeDefault}`. Then copy a profile that returns `SCMP_ACT_ERRNO` for `mkdir` and `mkdirat` to `/var/lib/kubelet/seccomp/profiles/no-mkdir.json` on a node and run a Pod with `type: Localhost`.
  - **Predict:** the `Seccomp:` value (0 disabled, 2 filter) in the first two Pods.
  - **Verify:** 0 and 2, unless the kubelet runs with `seccompDefault: true`; `mkdir /tmp/x` fails with `Operation not permitted` under your profile.
- [ ] **28.4 Falco catches a shell** *(Level: Core)*
  - **Do:** `helm install falco falcosecurity/falco -n falco --create-namespace --set driver.kind=modern_ebpf --set tty=true`. `kubectl exec -it <pod> -- sh`, then `cat /etc/shadow` (§16, §17).
  - **Predict:** which rules fire, and the delay from command to alert.
  - **Verify:** `Terminal shell in container` and a sensitive-file-read rule in `kubectl logs -n falco ds/falco`; the delay in ms from the event timestamps.
- [ ] **28.5 Who deleted my Pod?** *(Level: Core)*
  - **Do:** delete a Pod as alice (07.1). Answer from `auditlog` only: who, when, from which IP, with which client (§24).
  - **Verify:** a single `jq` filter that prints `user.username, sourceIPs, userAgent, requestReceivedTimestamp` for `verb=="delete"` on that Pod.
- [ ] **28.6 From detection to prevention** *(Level: Stretch)*
  - **Do:** install Tetragon; a `TracingPolicy` on file opens of `/etc/shadow` with `matchActions: [{action: Sigkill}]` (§18, §21). Repeat the `cat`.
  - **Verify:** the process is killed (exit 137) and Tetragon logs the event; Falco (detection) still only alerts.

**Checkpoint (closed book):**
1. What do PSA's `enforce`, `audit`, and `warn` modes each do?
2. What seccomp profile does a Pod get by default?
3. How does Tetragon's enforcement differ from Falco's alerting?
<details><summary>Answers</summary>

1. `enforce` rejects violating Pods; `audit` admits them and adds an annotation to the audit event; `warn` admits them and returns a warning to the client.
2. `Unconfined`, unless the kubelet runs with `seccompDefault: true`, which makes `RuntimeDefault` the default.
3. Falco observes syscalls and raises alerts after the fact. Tetragon can act in-kernel at the hook (for example SIGKILL or override a return value) before the operation completes.
</details>

---

## 29 — Pod Sandboxing  ([chapter](29-pod-sandboxing.md))
**Time:** ~3 h · **Needs:** `minikube start --container-runtime=containerd` + `minikube addons enable gvisor` (or runsc in the Linux VM); Kata only with nested virtualization (optional)

- [ ] **29.1 Wire up a RuntimeClass** *(Level: Core)*
  - **Do:** `kubectl get runtimeclass`; run two Pods, one with `runtimeClassName: gvisor`. In each: `uname -r`, `dmesg | head -3`, `cat /proc/self/status | grep Seccomp` (§4, §8).
  - **Predict:** the kernel version each reports.
  - **Verify:** the runc Pod shows the host kernel; the gVisor Pod shows gVisor's emulated kernel version and its own `dmesg` lines.
- [ ] **29.2 Where the process lives** *(Level: Core)*
  - **Do:** `minikube ssh`, then `ps -ef` and search for each Pod's app process (§9).
  - **Predict:** whether each app appears as a host process.
  - **Verify:** the runc app is a host PID; the gVisor app is not: you see `runsc` sandbox, gofer, and Sentry processes instead.
- [ ] **29.3 Syscall-heavy vs CPU-bound** *(Level: Core)*
  - **Goal:** measure where the sandbox costs (§12, §23).
  - **Do:** in both Pods, time `dd if=/dev/zero of=/dev/null bs=1 count=2000000` (syscall-bound) and `python3 -c "sum(i*i for i in range(10**7))"` (CPU-bound). Five runs each.
  - **Predict:** the gVisor/runc ratio for each.
  - **Verify:** a table of medians; the syscall-bound ratio is large, the CPU-bound one is near 1.
- [ ] **29.4 Cold start** *(Level: Core)*
  - **Do:** 10 × (create Pod → `kubectl wait --for=condition=Ready`) for each runtime, same image already pulled.
  - **Verify:** median and max for both; add the numbers to a "when to sandbox" note.
- [ ] **29.5 Find a compatibility gap** *(Level: Stretch)*
  - **Do:** try `fio --ioengine=io_uring`, `perf stat true`, or raw sockets under gVisor (§11, §29).
  - **Verify:** one thing that works on runc and fails on gVisor, with the error text. Also add `overhead.podFixed` to the RuntimeClass and see it in `kubectl describe node` requests (§6).

**Checkpoint (closed book):**
1. How does gVisor intercept a container's syscalls?
2. What does `overhead.podFixed` on a RuntimeClass change?
3. Name a case where a sandbox is the wrong tool.
<details><summary>Answers</summary>

1. Its Sentry, a userspace kernel, receives the syscalls through a platform (systrap, ptrace, or KVM) and implements them itself; a Gofer process mediates file access. Few syscalls reach the host kernel.
2. It is added to the Pod's resource requests for scheduling and quota, and to the Pod cgroup, to account for the sandbox's own footprint.
3. Syscall- or I/O-heavy workloads that are performance-critical, workloads that need host kernel features, or threats that are not kernel escapes (application bugs, stolen credentials, data exfiltration).
</details>

---

## 30 — Observability Internals  ([chapter](30-observability-internals.md))
**Time:** ~4 h · **Needs:** kind `lab`, kube-prometheus-stack

- [ ] **30.1 The kubelet's four telemetry endpoints** *(Level: Core)*
  - **Do:** series count of `kubeletmetrics lab-worker`, `kubeletmetrics lab-worker /cadvisor`, `kubeletmetrics lab-worker /resource` (`grep -vc '^#'`), and byte size of `kubectl get --raw /api/v1/nodes/lab-worker/proxy/stats/summary` (§3–§5).
  - **Predict:** which endpoint is largest and roughly how it scales with Pods.
  - **Verify:** four numbers; add 20 Pods and measure again to get series per Pod for `/cadvisor`.
- [ ] **30.2 cAdvisor vs the cgroup file** *(Level: Core)*
  - **Do:** during 21.1's throttled Pod, compare the delta of `container_cpu_cfs_throttled_periods_total` from `/metrics/cadvisor` with the delta of `nr_throttled` from `cpu.stat` over the same 60 s.
  - **Verify:** they match; you can name the cgroup file behind three more cAdvisor metrics.
- [ ] **30.3 kube-state-metrics cardinality** *(Level: Core)*
  - **Goal:** measure series per object (§7, §33).
  - **Do:** with kube-prometheus-stack running, `count({job="kube-state-metrics"})`; create 500 ConfigMaps and 100 Pods; measure again.
  - **Predict:** series added per ConfigMap and per Pod.
  - **Verify:** both numbers; `topk(10, count by (__name__)({job="kube-state-metrics"}))` names the biggest families.
- [ ] **30.4 Control-plane SLIs in PromQL** *(Level: Core)*
  - **Do:** write and run: apiserver p99 by verb excluding `WATCH|CONNECT` (`histogram_quantile(0.99, sum by (le, verb) (rate(apiserver_request_duration_seconds_bucket{verb!~"WATCH|CONNECT"}[5m])))`), scheduler attempt p99, etcd WAL fsync p99, workqueue depth by controller. Turn the first into a recording rule (§9–§12, §19, §36).
  - **Verify:** four numbers from your cluster and a `PrometheusRule` that loads (check `prometheus_rule_group_last_evaluation_timestamp_seconds`).
- [ ] **30.5 Container logs on disk** *(Level: Core)*
  - **Goal:** see the CRI log format and rotation (§25).
  - **Do:** a Pod that prints one 40,000-character line, then 50 MB of logs. On the node: `ls -la /var/log/pods/<ns>_<pod>_<uid>/<container>/` and `head -c 300 0.log`.
  - **Predict:** how the long line is stored, and what `kubectl logs` shows after rotation.
  - **Verify:** `<timestamp> stdout P ...` partial records ending with an `F` record; rotated files `0.log.<ts>` (compressed after the first), and `kubectl logs` shows only the current file.
- [ ] **30.6 Cardinality explosion** *(Level: Stretch)*
  - **Do:** an app that labels a counter with a random `user_id` per request; send 50k requests; watch `prometheus_tsdb_head_series` and Prometheus RSS (§33).
  - **Verify:** series and memory before and after; drop the label with `metric_relabel_configs` and confirm growth stops.

**Checkpoint (closed book):**
1. Where does metrics-server get its data, and who consumes it?
2. What does an `F` vs `P` mean in a CRI log line?
3. What drives kube-state-metrics cardinality?
<details><summary>Answers</summary>

1. From each kubelet's `/metrics/resource` (cAdvisor-derived); it serves the `metrics.k8s.io` aggregated API to `kubectl top`, the HPA and VPA. It is not a Prometheus data source.
2. `P` is a partial line (the runtime split a long line, at 16 KiB in containerd); `F` is the full or final fragment.
3. The number of objects times the metric families per kind, times label values (labels and annotations you allow-list add more).
</details>

---

## 31 — GitOps, Helm, and Kustomize  ([chapter](31-gitops-helm-kustomize.md))
**Time:** ~4 h · **Needs:** kind `lab`, `helm`, `kustomize`, Argo CD

- [ ] **31.1 Decode Helm's release storage** *(Level: Core)*
  - **Do:** install a chart, upgrade it twice. `kubectl get secrets -l owner=helm`; `kubectl get secret sh.helm.release.v1.<rel>.v1 -o jsonpath='{.data.release}' | base64 -d | base64 -d | gunzip | jq '{name, version, info: .info.status}, (.manifest | length)'` (§24).
  - **Predict:** how many release Secrets exist and what one contains.
  - **Verify:** one Secret per revision (capped by `--history-max`); each holds the full rendered manifest, values and chart metadata.
- [ ] **31.2 Drift and self-heal, timed** *(Level: Core)*
  - **Do:** install Argo CD; an `Application` with `automated: {prune: true, selfHeal: true}`. `kubectl scale deploy <app> --replicas=5`; poll replicas every second. Repeat with `selfHeal: false` (§8, §13).
  - **Predict:** time to revert, and what happens without self-heal.
  - **Verify:** revert delay in seconds; without self-heal the app shows `OutOfSync` and keeps 5 replicas.
- [ ] **31.3 Sync waves and hooks** *(Level: Core)*
  - **Do:** a `PreSync` hook Job, and three resources with `argocd.argoproj.io/sync-wave` `-1`, `0`, `1` (§9). Sync and read `creationTimestamp`s.
  - **Predict:** the order.
  - **Verify:** hook → wave −1 → 0 → 1, each wave waiting for the previous one to be healthy.
- [ ] **31.4 The fight over `spec.replicas`** *(Level: Core)*
  - **Goal:** reproduce the HPA vs GitOps conflict (§37).
  - **Do:** keep `replicas: 2` in Git, add an HPA that wants 4, enable self-heal. Count replica changes over 5 minutes (`kubectl get deploy -w`).
  - **Predict:** the pattern.
  - **Verify:** replicas oscillate between 2 and 4. Fix by removing `replicas` from the manifest or with `ignoreDifferences` on `/spec/replicas`; the oscillation stops.
- [ ] **31.5 Generator hashes trigger rollouts** *(Level: Stretch)*
  - **Do:** a `configMapGenerator` in Kustomize; change one value; `kustomize build | grep -A2 'kind: ConfigMap'`; apply and watch ReplicaSets. Then `helm template . | kubectl diff -f -` as a render-then-apply check (§28, §31).
  - **Verify:** a new ConfigMap name suffix and a new ReplicaSet; the diff shows exactly the changed fields.

**Checkpoint (closed book):**
1. Where does Helm 3 keep release state, and how is it encoded?
2. What is the difference between push and pull GitOps?
3. Why does changing a `configMapGenerator` value roll a Deployment?
<details><summary>Answers</summary>

1. In Secrets of type `helm.sh/release.v1` in the release namespace, one per revision: gzipped, base64-encoded JSON, then base64-encoded again by the Secret.
2. Push: CI holds cluster credentials and applies. Pull: an agent in the cluster fetches from Git and reconciles continuously, so it also detects and corrects drift.
3. The generated name includes a hash of the content, so the Pod template's reference changes, which is a template change and makes a new ReplicaSet.
</details>

---

## 32 — Cluster Lifecycle and Day 2  ([chapter](32-cluster-lifecycle-and-day2.md))
**Time:** ~4 h · **Needs:** kind `lab` (destructive tasks: recreate afterwards), MinIO + Velero for the stretch

- [ ] **32.1 Read the PKI** *(Level: Core)*
  - **Do:** `docker exec lab-control-plane kubeadm certs check-expiration`; `ls /etc/kubernetes/pki /etc/kubernetes/pki/etcd`; `openssl x509 -in /etc/kubernetes/pki/apiserver.crt -noout -ext subjectAltName -enddate` (§5, §27, §28).
  - **Predict:** leaf vs CA lifetimes, and the SANs on the apiserver cert.
  - **Verify:** leaves ~1 year, CAs ~10 years; the SANs include `kubernetes.default.svc`, the Service ClusterIP (first IP of the service CIDR) and the node IP. The etcd CA is separate from the cluster CA.
- [ ] **32.2 Renew a cert and prove who serves it** *(Level: Core)*
  - **Do:** record `openssl s_client -connect 127.0.0.1:6443 </dev/null 2>/dev/null | openssl x509 -noout -enddate` inside the node. `kubeadm certs renew apiserver`; check again; then restart the apiserver static Pod (move its manifest out and back) and check again.
  - **Predict:** the served expiry after renew and after restart.
  - **Verify:** unchanged after renew, new after restart: the file changed, the process did not reload it.
- [ ] **32.3 A drain that cannot finish** *(Level: Core)*
  - **Goal:** see drain use the Eviction API against a PDB (§13, §14).
  - **Do:** a 2-replica Deployment pinned to `lab-worker`, and a PDB with `minAvailable: 2`. `kubectl drain lab-worker --ignore-daemonsets --delete-emptydir-data --timeout=60s`. Count eviction attempts: `auditlog | jq -r 'select(.objectRef.subresource=="eviction") | .responseStatus.code' | sort | uniq -c`.
  - **Predict:** the drain result and the response code of each eviction.
  - **Verify:** the drain times out; evictions return 429 repeatedly. Change to `minAvailable: 1` and the drain completes one Pod at a time.
- [ ] **32.4 etcd snapshot and point-in-time restore** *(Level: Core, destructive)*
  - **Goal:** rewind the cluster and see the side effects (§16, §17).
  - **Do:** `kubectl create cm before`; `ketcd snapshot save /var/lib/etcd/snap.db`; `kubectl create cm after`. `kubectl -n kube-system exec etcd-lab-control-plane -- etcdutl snapshot restore /var/lib/etcd/snap.db --data-dir /var/lib/etcd/restored --name lab-control-plane --initial-cluster lab-control-plane=https://<node-ip>:2380 --initial-advertise-peer-urls https://<node-ip>:2380`. Move the apiserver manifest out; `sed` the etcd manifest's `--data-dir` to `/var/lib/etcd/restored`; wait for etcd; move the apiserver back.
  - **Predict:** which ConfigMaps exist, and what happens to `resourceVersion` compared with what running controllers have cached.
  - **Verify:** `before` exists, `after` is gone; a ConfigMap created now gets a `resourceVersion` lower than `after` had (unless you restore with `--bump-revision` and `--mark-compacted`). Restart the controller-manager and scheduler and write why the chapter says to.
- [ ] **32.5 A skew table for your next upgrade** *(Level: Core)*
  - **Do:** `kubectl version`, `kubectl get nodes -o wide`, and the control-plane image tags. `kubeadm upgrade plan` inside the control-plane node (§8, §9).
  - **Verify:** a table of each component's version today, and which versions each could be at after upgrading the apiserver by one minor, per the skew policy.
- [ ] **32.6 Velero backup and restore** *(Level: Stretch)*
  - **Do:** MinIO in the cluster as the object store; `velero install` with the AWS plugin pointed at MinIO; back up a namespace with a PVC (file-system backup); delete the namespace; restore (§20–§22).
  - **Verify:** the restored app serves the same data; backup and restore durations written down, i.e. your RTO for this namespace.

**Checkpoint (closed book):**
1. How far may kubelets lag the apiserver's minor version?
2. After an etcd restore, why restart the other control-plane components?
3. How does `kubectl drain` respect PodDisruptionBudgets?
<details><summary>Answers</summary>

1. Up to 3 minors older (1.28+; 2 before), never newer.
2. The restore moves `resourceVersion` backwards. Watch caches and informers hold newer versions and can miss or misorder events, so they must restart and relist.
3. It evicts through the Eviction subresource; the apiserver refuses (429) any eviction that would violate a PDB, and drain retries until its timeout.
</details>

---

## 33 — Edge and Special Distributions  ([chapter](33-edge-and-special-distributions.md))
**Time:** ~3 h · **Needs:** `k3d`, `sqlite3`, kind `lab` for comparison

- [ ] **33.1 How small is small?** *(Level: Core)*
  - **Do:** `time k3d cluster create edge --agents 2`; `docker stats --no-stream` for `k3d-edge-server-0` and `lab-control-plane`; `docker exec k3d-edge-server-0 ps -o pid,rss,args` (§4, §5).
  - **Predict:** create time, the memory ratio server vs kind control plane, and how many control-plane processes k3s runs.
  - **Verify:** three numbers; apiserver, scheduler and controller-manager run inside one `k3s server` process.
- [ ] **33.2 kine: etcd semantics on SQLite** *(Level: Core)*
  - **Goal:** compare with chapter 04's MVCC (§4).
  - **Do:** create a ConfigMap in k3d and update it twice. `docker cp k3d-edge-server-0:/var/lib/rancher/k3s/server/db ./k3sdb`; `sqlite3 k3sdb/state.db "select id, name, created, deleted, prev_revision from kine where name like '/registry/configmaps/default/%' order by id"`.
  - **Predict:** how many rows the ConfigMap has and which column is the revision.
  - **Verify:** one row per version (history kept until compaction); `id` is the revision and equals the object's `resourceVersion`.
- [ ] **33.3 Cut an edge node off** *(Level: Core)*
  - **Goal:** see what autonomy you do and do not get (§17, §23).
  - **Do:** a 2-replica Deployment spread over both agents. `docker network disconnect k3d-edge k3d-edge-agent-1` for 8 minutes. Watch Pods from the server; check `docker exec k3d-edge-agent-1 crictl ps` during the partition. Reconnect.
  - **Predict:** node status, API Pod status at 1 and 6 minutes, and whether the containers keep running on the cut-off node.
  - **Verify:** NotReady at ~40 s; replacement Pods after the 300 s toleration; the original containers keep running locally until reconnection, then are removed.
- [ ] **33.4 Explain it** *(Level: Core)*
  - **Do:** a 5-sentence note to an IoT team: embedded cluster per site (K3s) vs centralized control plane with remote nodes (KubeEdge/OpenYurt), using your 33.3 numbers (§3, §11).
- [ ] **33.5 What was stripped and bundled** *(Level: Stretch)*
  - **Do:** `kubectl get pods -A` in k3d; `docker exec k3d-edge-server-0 k3s --version`; the binary size; list what is embedded (containerd, flannel, CoreDNS, Traefik, ServiceLB, local-path, metrics-server) and what was removed (in-tree cloud providers, alpha APIs) (§4, §28).
  - **Verify:** a two-column table with the evidence for each item.

**Checkpoint (closed book):**
1. What replaces etcd in a default single-server k3s, and how does it keep resourceVersions?
2. An edge node loses its control plane for an hour. What happens to its Pods?
3. What are the two architectural patterns for edge Kubernetes?
<details><summary>Answers</summary>

1. kine over SQLite: an SQL table where each write is a new row, and the auto-increment `id` is the revision. It emulates the etcd API, including watch.
2. The containers keep running; the kubelet cannot report. The control plane marks the node NotReady and, after the toleration, deletes the Pods from the API and reschedules them elsewhere. On reconnect the kubelet removes the local copies.
3. An embedded cluster per site (a full small control plane at the edge), or a centralized control plane with remote edge nodes and an edge-side agent for autonomy.
</details>

---

## 34 — Custom Schedulers and the Scheduler Framework  ([chapter](34-custom-schedulers-and-scheduler-framework.md))
**Time:** ~6 h · **Needs:** kind `lab`, Go, Python, the second scheduler from 09.4 · **First:** chapter 09 labs; for the GPU version see [k8s-learn/gpu-platform-tasks.md](../k8s-learn/gpu-platform-tasks.md) Project 5

- [ ] **34.1 A scheduler extender in Python** *(Level: Core)*
  - **Goal:** extend scheduling over HTTP (§20).
  - **Do:** a server with `/filter` (keep only nodes labelled `zone=green`) and `/prioritize` (score by any rule you like), using the `ExtenderArgs`/`ExtenderFilterResult` JSON shapes. Add to the `binpack` scheduler's config: `extenders: [{urlPrefix: "http://ext.kube-system:8000", filterVerb: filter, prioritizeVerb: prioritize, weight: 1, nodeCacheCapable: false}]`.
  - **Predict:** the per-Pod latency the extender adds, and what happens when it is down with `ignorable: false`.
  - **Verify:** placements obey your filter; `scheduler_scheduling_attempt_duration_seconds` rises by your measured RTT; with the extender down, Pods stay Pending with an extender error. Set `ignorable: true` and they schedule.
- [ ] **34.2 A Filter + Score plugin in Go** *(Level: Core)*
  - **Goal:** build an out-of-tree plugin (§4, §6).
  - **Do:** a `main.go` calling `app.NewSchedulerCommand(app.WithPlugin(Name, New))`; `Filter` rejects nodes annotated `lab/maintenance=true`; `Score` prefers nodes with fewer Pods of the same `app` label, computed in `PreScore` and stored in `CycleState`. Build, `kind load docker-image`, deploy as `lab-scheduler` with a profile enabling your plugin.
  - **Verify:** annotated nodes never get Pods; 6 replicas spread per your score; `scheduler_plugin_execution_duration_seconds{plugin="<Name>"}` has samples.
- [ ] **34.3 Gang scheduling with Permit** *(Level: Core)*
  - **Goal:** see all-or-nothing placement (§7, §8).
  - **Do:** install the scheduler-plugins build with Coscheduling as a second scheduler; a `PodGroup` with `minMember: 3`; three Pods that only two nodes' worth of CPU can hold (size requests so only 2 fit).
  - **Predict:** how many Pods bind under Coscheduling and under the default scheduler.
  - **Verify:** Coscheduling binds 0 (Pods wait at Permit, then time out and retry); the default scheduler binds 2 and leaves 1 Pending, holding resources a smaller job could use.
- [ ] **34.4 Kueue admits, the scheduler places** *(Level: Core)*
  - **Do:** install Kueue; a `ClusterQueue` with a 4-CPU quota and a `LocalQueue`; submit three Jobs of 2 CPU each with the `kueue.x-k8s.io/queue-name` label (§21, §22).
  - **Predict:** which Jobs run, and what state the third is in.
  - **Verify:** `kubectl get workloads` shows 2 admitted and 1 pending; the third Job is `suspend: true` and has no Pods until one finishes.
- [ ] **34.5 Benchmark your plugin** *(Level: Stretch)*
  - **Do:** with `kwokctl` (500 nodes), schedule 5000 Pods with the default profile and with your plugin (§28).
  - **Verify:** Pods/s for both, and the p99 of your plugin's extension points.

**Checkpoint (closed book):**
1. Compare an extender, a plugin, and a replacement scheduler in one line each.
2. How does the Permit extension point implement gang scheduling?
3. What does Kueue do that the scheduler does not?
<details><summary>Answers</summary>

1. Extender: an HTTP call per Pod at a few points, no shared cache, easy to write, slow and a failure dependency. Plugin: compiled into the scheduler, every extension point, shared snapshot and CycleState. Replacement: you own everything, including preemption and correctness.
2. Each member Pod reaches Permit and gets `Wait` while it holds its reservation; when `minMember` Pods are waiting, all are allowed to bind. On timeout all are rejected and their reservations released.
3. Admission-time queueing against quotas: it keeps whole Jobs suspended until capacity is available (with cohorts, borrowing, fair sharing), instead of creating Pods that sit Pending.
</details>

---

## 35 — Performance, Scaling, and Tuning  ([chapter](35-performance-scaling-and-tuning.md))
**Time:** ~5 h · **Needs:** kind `lab`, `kwokctl`, Go (`go tool pprof`), Linux VM for the stretch

- [ ] **35.1 Profile the apiserver** *(Level: Core)*
  - **Do:** start the LIST flood from 05.4 (without the APF limit). `kubectl get --raw '/debug/pprof/profile?seconds=30' > api.pprof`; `go tool pprof -top api.pprof | head -25`; also a heap profile (`/debug/pprof/heap`) (§17).
  - **Predict:** the top three cumulative CPU consumers.
  - **Verify:** the list, typically serialization, conversion and copying on the LIST path; explain each in one line.
- [ ] **35.2 Find the noisy client** *(Level: Core)*
  - **Goal:** run the playbook of §18–§19.
  - **Do:** run 08.1's poller plus normal activity. From `auditlog`, rank clients: `jq -r '[.user.username, .userAgent] | @tsv' | sort | uniq -c | sort -rn | head`. Cross-check with `apiserver_flowcontrol_dispatched_requests_total` by flow schema.
  - **Verify:** the poller is the top client by request count; you can name the FlowSchema it lands in.
- [ ] **35.3 The huge-object anti-pattern** *(Level: Core)*
  - **Goal:** measure what big objects do to the control plane (§27).
  - **Do:** create 100 ConfigMaps of ~900 KiB. Record the apiserver's memory (`kubectl top pod -n kube-system`), etcd `dbSize` (`ketcd endpoint status -w json`), and `time kubectl get cm -A -o json | wc -c`. Then delete them and compact + defrag.
  - **Predict:** etcd size growth and the LIST response size.
  - **Verify:** your numbers; apiserver memory rises by more than the raw data (watch cache plus decoded copies).
- [ ] **35.4 1000 nodes on a laptop** *(Level: Core)*
  - **Do:** `kwokctl create cluster`; `kwokctl scale node --replicas 1000`; a Deployment of 10,000 Pods. Measure time to all scheduled, etcd db size, and `kubectl get pods -A` with `--chunk-size=0` vs the default 500 (§28).
  - **Predict:** schedule time and the difference between chunked and unchunked LIST.
  - **Verify:** your numbers, plus apiserver p99 for LIST pods from its `/metrics`.
- [ ] **35.5 conntrack exhaustion** *(Level: Stretch)*
  - **Do:** on the Linux VM, lower `net.netfilter.nf_conntrack_max` to 2000; run `hey -c 50 -n 20000` against a container behind a published port; watch `dmesg` and `conntrack -C` (§31).
  - **Predict:** the error rate.
  - **Verify:** `nf_conntrack: table full, dropping packet` in `dmesg` and failed requests; restore the value.

**Checkpoint (closed book):**
1. Why is etcd defragmentation a common cause of outages?
2. Name the three official scalability SLO families (§1.2).
3. What makes a controller's informer memory grow?
<details><summary>Answers</summary>

1. Defrag blocks the member while it rewrites the database. Run on the leader or on all members at once, it stalls writes and can trigger elections and apiserver timeouts.
2. Pod startup latency, API call latency, and in-cluster network programming latency.
3. It caches every object of every watched type in its scope, so memory ≈ object count × object size. Unscoped informers on Pods, Secrets or ConfigMaps in a big cluster are the usual cause.
</details>

---

## 36 — Garbage Collection and Object Lifecycle  ([chapter](36-garbage-collection-and-object-lifecycle.md))
**Time:** ~3 h · **Needs:** kind `lab` · **First:** [k8s-learn/api-machinery-tasks.md](../k8s-learn/api-machinery-tasks.md) Level 3

- [ ] **36.1 The foreground deletion dance** *(Level: Core)*
  - **Goal:** see the owner wait for its dependents (§7, §23).
  - **Do:** a 2-replica Deployment; add finalizer `lab/hold` to one Pod. `kubectl delete deploy web --cascade=foreground --wait=false`. Read the Deployment's and ReplicaSet's `deletionTimestamp` and `finalizers`.
  - **Predict:** what exists after 30 s.
  - **Verify:** Deployment and RS still exist with `foregroundDeletion`; the held Pod is `Terminating`. Remove `lab/hold` and everything goes, bottom-up.
- [ ] **36.2 A cross-namespace owner** *(Level: Core)*
  - **Goal:** break the owner-namespace rule (§17).
  - **Do:** a ConfigMap `cm-a` in `ns1`; a ConfigMap in `ns2` whose `ownerReferences` points at `cm-a`'s name and UID.
  - **Predict:** what the garbage collector does to the `ns2` ConfigMap.
  - **Verify:** it is deleted (the owner is looked up in the dependent's namespace and not found), with an `OwnerRefInvalidNamespace` event.
- [ ] **36.3 Diagnose a stuck namespace** *(Level: Core)*
  - **Do:** a CRD, one CR with finalizer `lab/never` in namespace `stuck`, then `kubectl delete ns stuck --wait=false`. Read `.status.conditions`. Find the blocking object with `kubectl api-resources --verbs=list --namespaced -o name | xargs -n1 kubectl get -n stuck --ignore-not-found --show-kind` (§14, §32).
  - **Verify:** the conditions name `NamespaceContentRemaining` and `NamespaceFinalizersRemaining`; your one-liner finds the CR; removing its finalizer lets the namespace go. Note the escape hatch you did **not** use (editing the namespace's finalize subresource) and why it leaks data.
- [ ] **36.4 TTL-after-finished, timed** *(Level: Core)*
  - **Do:** a Job with `ttlSecondsAfterFinished: 30`; record the completion time and the deletion time. Read the GC metrics with `cpmetrics 10257 | grep -E '^garbagecollector|ttl_after_finished'` (§19, §34).
  - **Verify:** deletion ≈ 30 s after completion; the Job's Pods are removed by the GC as dependents.
- [ ] **36.5 An ownership cycle** *(Level: Stretch)*
  - **Do:** ConfigMaps A and B each owning the other with `blockOwnerDeletion: true`. Delete A with `--cascade=foreground` (§26).
  - **Predict:** whether either is ever deleted.
  - **Verify:** record the outcome and the GC's handling (events, and the objects' finalizers over 2 minutes).

**Checkpoint (closed book):**
1. What do Background, Foreground and Orphan deletion each do?
2. What does `blockOwnerDeletion: true` do?
3. Why can't a namespaced object have an owner in another namespace?
<details><summary>Answers</summary>

1. Background: the owner is deleted now and the GC deletes dependents afterwards. Foreground: the owner gets `foregroundDeletion` and stays until dependents that block it are gone. Orphan: dependents' ownerReferences are removed and they stay.
2. In foreground deletion, the owner is not removed until this dependent is deleted. Setting it also requires delete permission on the owner.
3. An ownerReference has no namespace field; for a namespaced dependent the owner is resolved in the dependent's own namespace. Cross-namespace ownership would also break namespace isolation for deletion.
</details>

---

## 37 — Cloud Provider Integration  ([chapter](37-cloud-provider-integration.md))
**Time:** ~3 h · **Needs:** kind `lab`, `cloud-provider-kind`

- [ ] **37.1 A LoadBalancer on a laptop** *(Level: Core)*
  - **Goal:** watch a service controller do its job (§5, §11).
  - **Do:** run `cloud-provider-kind` (it needs the Docker socket). Create a `type: LoadBalancer` Service; `kubectl get svc -w`; `docker ps` for the new LB container; read `.metadata.finalizers` and `.status.loadBalancer`.
  - **Predict:** what fills `EXTERNAL-IP`, which finalizer appears, and what `docker ps` shows.
  - **Verify:** an IP on the kind Docker network; `service.kubernetes.io/load-balancer-cleanup`; a proxy container that answers `curl <external-ip>`.
- [ ] **37.2 Delete with the controller down** *(Level: Core)*
  - **Do:** stop `cloud-provider-kind`, delete the Service, wait 60 s, start it again (§11, §43).
  - **Predict:** the Service's state while the controller is down, and what happens to the LB container.
  - **Verify:** the Service stays `Terminating` (finalizer); after restart the LB container is removed and then the Service. One sentence on why the finalizer exists.
- [ ] **37.3 The health check node port** *(Level: Core)*
  - **Goal:** see how a cloud LB learns where local endpoints are (§12, §13).
  - **Do:** a LoadBalancer Service with `externalTrafficPolicy: Local` and 1 replica. Read `.spec.healthCheckNodePort`. From a node: `curl -s http://<each-node-ip>:<hc-port>/healthz`.
  - **Predict:** each node's response.
  - **Verify:** 200 with `localEndpoints: 1` on the Pod's node, 503 with 0 elsewhere (served by kube-proxy).
- [ ] **37.4 providerID and the uninitialized taint** *(Level: Stretch)*
  - **Do:** `kubectl get nodes -o custom-columns=N:.metadata.name,P:.spec.providerID`. Then create a kind cluster whose kubelets run with `cloud-provider: external` (`kubeletExtraArgs`) and look at node taints before any cloud controller runs (§8, §9).
  - **Predict:** the providerID format, and which Pods can schedule on the new cluster.
  - **Verify:** nodes carry `node.cloudprovider.kubernetes.io/uninitialized:NoSchedule`; only tolerating Pods run. Record whether `cloud-provider-kind` initializes the nodes, and what it sets.
- [ ] **37.5 Explain it** *(Level: Core)*
  - **Do:** write the IRSA flow (§20) as 6 numbered steps from Pod start to an AWS API call, naming the token audience, the OIDC discovery URL and the STS call. Check it against the chapter. (Running it needs an AWS account: optional, cloud.)

**Checkpoint (closed book):**
1. Which controllers does the cloud-controller-manager run?
2. What is the `uninitialized` taint for?
3. With `externalTrafficPolicy: Local`, how does the cloud LB avoid nodes without endpoints?
<details><summary>Answers</summary>

1. The cloud node controller (and node lifecycle), the route controller, and the service (LoadBalancer) controller.
2. A kubelet started with `--cloud-provider=external` registers its node with it. The CCM's node controller fills in addresses, providerID, and zone and instance labels, then removes the taint, so workloads do not land on a half-initialized node.
3. kube-proxy serves `healthCheckNodePort`, which returns 200 only on nodes with a ready local endpoint. The LB health-checks that port and sends traffic only to healthy nodes.
</details>

---

## 38 — Building a Kubernetes from Scratch  ([chapter](38-building-a-kubernetes-from-scratch.md))
**Time:** ~3–5 days · **Needs:** Linux VM (root), Python 3.12+ or Go · **First:** labs 00, 01, 04, 08, 09, 10

Build `minik8s` in the chapter's phase order (§5). Each phase must run end to end before the next. These tasks add the measurements and failure tests the chapter leaves to you.

- [ ] **38.1 Phase 1: a container launcher** *(Level: Core)*
  - **Do:** `minik8s-run <rootfs> <cmd>`: new PID, mount, UTS, net namespaces; `pivot_root` into a busybox rootfs; mount `/proc`; a cgroup with `memory.max` from a flag (§6).
  - **Verify:** inside, `ps` shows PID 1 and `mount` shows only your rootfs; a 200 MB allocation under a 100 MB limit is OOM-killed (`memory.events`). Start latency over 20 runs vs `runc run` from 01.1.
- [ ] **38.2 Phase 3: minikv with revisions, watch, compaction** *(Level: Core)*
  - **Do:** a global revision counter, per-key history, `watch(prefix, from_rev)`, `compact(rev)` (§8).
  - **Verify:** a property test: 1000 random puts/deletes, then replaying `watch("", 0)` rebuilds the exact final state; a watch from a compacted revision raises your equivalent of "required revision has been compacted" (compare 04.3).
- [ ] **38.3 Phases 4–5: apiserver with optimistic concurrency** *(Level: Core)*
  - **Do:** REST for Pods and ReplicaSets over minikv, `resourceVersion` = mod revision, a streaming watch endpoint (§9, §10).
  - **Verify:** `curl -N` receives events in order; an update carrying a stale `resourceVersion` returns 409; two concurrent updaters never lose a write (run 1000 increments from 4 clients).
- [ ] **38.4 Phases 6–7: kubelet and scheduler** *(Level: Core)*
  - **Do:** the scheduler filters by free CPU and binds; the kubelet watches Pods bound to its node, runs them with 38.1, and writes status (§11, §12).
  - **Verify:** POST a Pod → Running; measure create-to-Running latency. `kill -9` the container: the kubelet restarts it and increments a restart count.
- [ ] **38.5 Phase 8: a ReplicaSet controller with a workqueue** *(Level: Core)*
  - **Do:** informer → dedup queue → reconcile, level-triggered (§13).
  - **Verify:** scale 3 → 10 → 2 converges; kill the controller mid-scale and restart it: it converges without duplicates.
- [ ] **38.6 Phases 9–10: networking and Services** *(Level: Stretch)*
  - **Do:** your CNI from 15.5 and an iptables DNAT proxy (§14, §15).
  - **Verify:** Pod-to-Pod ping and Pod-to-VIP curl; compare your line counts per component with the table in §17.

**Checkpoint (closed book):**
1. What is the minimum set of components for "a Pod runs on a node"?
2. Why does watch need a global revision?
3. Where does real Kubernetes spend most of its code that your toy skipped?
<details><summary>Answers</summary>

1. A store, an API server, a kubelet, and a container runtime. A scheduler is optional if you set `nodeName` yourself.
2. So a client can resume from exactly where it stopped, with a total order across keys and no gaps; and so a LIST gives a consistent point to start watching from.
3. API machinery (versioning, conversion, validation, defaulting, generated code), security (authn, authz, admission), HA and scale (watch cache, APF, leader election), and pluggable integrations (CRI, CNI, CSI, cloud providers). See §17 and §22.
</details>

---

## 39 — Dockerfile Best Practices  ([chapter](39-dockerfile-staff-level-best-practices.md))
**Time:** ~2 h · **Needs:** Docker with BuildKit, `dive`, local `registry:2`

- [ ] **39.1 Instruction order vs rebuild time** *(Level: Core)*
  - **Do:** a Python app with 20 dependencies. Dockerfile A: `COPY . .` then `pip install`. Dockerfile B: copy the requirements file, install, then `COPY . .`. Change one line of source and time the rebuild of each (§4, §5).
  - **Predict:** both rebuild times.
  - **Verify:** A reinstalls everything; B reuses the install layer (`CACHED` in the build output). Write the two times.
- [ ] **39.2 Multi-stage size** *(Level: Core)*
  - **Do:** a single-stage image with build tools vs a multi-stage image whose final stage is slim or distroless. `docker images`; `dive <image> --ci` (§6, §17).
  - **Predict:** the size ratio.
  - **Verify:** both sizes and dive's efficiency score.
- [ ] **39.3 Cache mounts** *(Level: Core)*
  - **Do:** add one dependency and rebuild with and without `RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt` (§8).
  - **Predict:** the rebuild time with the mount.
  - **Verify:** both times; the mount avoids re-downloading unchanged wheels, and the cache is not in the image (`dive`).
- [ ] **39.4 Secret mounts leave no trace** *(Level: Core)*
  - **Do:** use a token as `ARG TOKEN` in a `RUN`; `docker history --no-trunc` the image. Redo with `RUN --mount=type=secret,id=token` and `docker build --secret id=token,src=token.txt`. `docker save` both and grep the tars for the token (§8).
  - **Verify:** the ARG version shows the token in history; the secret-mount version has it nowhere.
- [ ] **39.5 Remote build cache** *(Level: Stretch)*
  - **Do:** `docker buildx build --cache-to type=registry,ref=localhost:5000/cache,mode=max --cache-from type=registry,ref=localhost:5000/cache .`; then `docker builder prune -af` and rebuild with `--cache-from` only (§15).
  - **Verify:** cold, warm-local and warm-remote build times.

**Checkpoint (closed book):**
1. What invalidates a layer's build cache?
2. What does a multi-stage build remove from the final image?
3. How does a BuildKit secret mount differ from a build `ARG`?
<details><summary>Answers</summary>

1. A change to the instruction, to the content checksum of files it copies, or to any earlier layer. Every later layer is rebuilt too.
2. Everything not explicitly copied from build stages: compilers, headers, package caches, source and test files.
3. The secret is mounted only for that `RUN` and is never written to a layer or the image history; an `ARG` value is recorded in history and can end up in layers.
</details>

---

## 40 — Docker Anti-Patterns and Bad Configs  ([chapter](40-docker-anti-patterns-and-bad-configs.md))
**Time:** ~2 h · **Needs:** Docker (on a Linux VM for 40.4)

- [ ] **40.1 Recover a "deleted" secret** *(Level: Core)*
  - **Do:** `COPY id_rsa /root/.ssh/id_rsa`, then `RUN rm /root/.ssh/id_rsa`. `docker save`, extract the layers, and recover the key (§3).
  - **Predict:** how many commands it takes.
  - **Verify:** the key is back on your disk; `diff` it with the original.
- [ ] **40.2 PID 1 and `docker stop`** *(Level: Core)*
  - **Do:** the same Python app (it exits on SIGTERM) with `CMD ["python","app.py"]`, with `CMD ["sh","-c","python app.py; echo exited"]` (the second command stops the shell from exec-ing Python), and the wrapper plus `docker run --init`. `time docker stop` each (§7). Also try plain shell form `CMD python app.py` and note whether your base image's `sh` execs the single command.
  - **Predict:** the three stop times.
  - **Verify:** exec form stops in well under a second; the wrapper takes the full 10 s and exits 137 (`sh` as PID 1 ignores SIGTERM and does not forward it); with `--init`, `sh` is no longer PID 1, so SIGTERM kills it and the stop is fast; check whether Python logged a clean shutdown or died with the namespace.
- [ ] **40.3 Zombies** *(Level: Core)*
  - **Do:** a PID 1 Python script that runs `subprocess.run(["sh","-c","sleep 1 &"])` 20 times (each shell exits and orphans its `sleep`, which is re-parented to PID 1), then sleeps forever. After 5 s, `docker exec <c> ps -o pid,ppid,stat,comm`. Repeat with `--init` (§7).
  - **Predict:** the number of `Z` entries in each case.
  - **Verify:** 20 defunct `sleep` entries whose parent is PID 1 without an init; 0 with tini as PID 1, because tini reaps orphans.
- [ ] **40.4 The Docker socket is root on the host** *(Level: Core)*
  - **Do:** on your Linux VM, `docker run -v /var/run/docker.sock:/var/run/docker.sock docker:cli docker run --rm -v /:/host alpine head -1 /host/etc/shadow` (§14).
  - **Predict:** whether it works.
  - **Verify:** it prints the host's shadow line. Write the one-line rule for CI runners.
- [ ] **40.5 Logs that eat the disk** *(Level: Stretch)*
  - **Do:** a container printing 10k lines/s with the default `json-file` driver; measure `/var/lib/docker/containers/<id>/<id>-json.log` growth per minute. Rerun with `--log-opt max-size=10m --log-opt max-file=3`. Also build with a 1 GB file in the context, with and without `.dockerignore`, and read the context transfer size (§10, §11).
  - **Verify:** MB/min unbounded vs capped at 30 MB; context size before and after.

**Checkpoint (closed book):**
1. Why doesn't `RUN rm secret` in a later layer protect the secret?
2. Why does a shell-form `CMD` make `docker stop` take 10 seconds?
3. Why is mounting `docker.sock` equivalent to root on the host?
<details><summary>Answers</summary>

1. The earlier layer still contains the file; anyone who can pull the image can extract it.
2. `sh` is PID 1 and does not forward SIGTERM to the app, and PID 1 has no default signal handlers, so nothing stops until the 10 s timeout and SIGKILL.
3. The daemon runs as root and does whatever the socket asks, including starting a privileged container with the host's `/` mounted.
</details>

---

## 41 — Docker Compose Deep Dive  ([chapter](41-docker-compose-deep-dive.md))
**Time:** ~2 h · **Needs:** Docker Compose v2, Postgres 17 image

- [ ] **41.1 Predict the merged model** *(Level: Core)*
  - **Do:** `compose.yaml` plus `compose.override.yaml`, overriding `command`, adding one `environment` key, and adding one `ports` entry. Write the merged service by hand, then `docker compose config` (§12).
  - **Verify:** diff your prediction against the output: `command` replaced, `environment` merged by key, `ports` combined.
- [ ] **41.2 Startup order vs readiness** *(Level: Core)*
  - **Do:** `db: postgres:17` and an `app` that connects once at startup and exits non-zero on failure. Run `docker compose up -d` 10 times (with `down -v` between) using `depends_on: [db]`, then with a `pg_isready` healthcheck and `condition: service_healthy` (§10).
  - **Predict:** failures out of 10 in each setup.
  - **Verify:** both counts; `service_started` only waits for the container to start, not for Postgres to accept connections.
- [ ] **41.3 Networks and the embedded DNS** *(Level: Core)*
  - **Do:** `frontend` and `backend` networks; `web` on both, `db` on backend, `proxy` on frontend. From `proxy`: `getent hosts db` and `getent hosts web`; `cat /etc/resolv.conf` (§5).
  - **Predict:** which names resolve from `proxy`.
  - **Verify:** `web` resolves, `db` does not; the resolver is `127.0.0.11`.
- [ ] **41.4 Project identity and scale** *(Level: Core)*
  - **Do:** `docker compose -p a up -d` and `-p b up -d` with the same file; `docker ps --format '{{.Names}}'`, `docker network ls`, `docker volume ls`, and `docker ps --filter label=com.docker.compose.project=a`. Then `docker compose -p a up -d --scale web=3` with `ports: ["8080:80"]` (§3, §16).
  - **Predict:** resource names, and the result of the scale.
  - **Verify:** everything is prefixed with the project; the scale fails on the fixed host port (use a port range or no host port).
- [ ] **41.5 Watch mode** *(Level: Stretch)*
  - **Do:** `develop.watch` with a `sync` rule for source and a `rebuild` rule for the lockfile; time edit → visible change for each (§15).
  - **Verify:** both latencies.

**Checkpoint (closed book):**
1. What does it take for `depends_on` to wait until a dependency is ready?
2. How does Compose name and find the resources of a project?
3. How do override files merge `command`, `environment` and `ports`?
<details><summary>Answers</summary>

1. A healthcheck on the dependency and `condition: service_healthy` in the long `depends_on` form.
2. Every container, network and volume is prefixed with the project name and labelled `com.docker.compose.project`; Compose finds them by label.
3. `command` (a scalar-like field) is replaced; `environment` merges by key; `ports` entries are combined.
</details>

---

## 42 — Compose vs Swarm vs Kubernetes  ([chapter](42-compose-vs-swarm-vs-kubernetes.md))
**Time:** ~3 h · **Needs:** Docker (Swarm mode on the Linux VM), kind `lab`

- [ ] **42.1 Kill the same container three ways** *(Level: Core)*
  - **Do:** a `web` + `redis` app run with Compose (`restart: unless-stopped`), as a Swarm stack (`docker swarm init; docker stack deploy -c compose.yaml app`), and on kind. `docker kill` the web container in each (for kind, `crictl stop` it on the node) and poll until it serves again (§3, §15).
  - **Predict:** recovery time for each, and which one replaces rather than restarts.
  - **Verify:** three recovery times. Now `docker compose stop web` and `docker service scale app_web=0` and note which of the three systems reconciles back without you.
- [ ] **42.2 Rolling updates under load** *(Level: Core)*
  - **Do:** a curl loop at 20 req/s while updating the image: Swarm with `update_config: {parallelism: 1, delay: 5s, order: start-first}`, Kubernetes with `maxSurge: 1, maxUnavailable: 0` and a readiness probe, Compose with `docker compose up -d` (§7).
  - **Predict:** failed requests in each.
  - **Verify:** three error counts; explain the Compose number.
- [ ] **42.3 The routing mesh** *(Level: Stretch)*
  - **Do:** publish a Swarm service on port 8080 with 1 replica on a 2-node Swarm (two VMs or Docker-in-Docker); curl both nodes. Inspect `docker network inspect ingress` (§4, §6).
  - **Verify:** both nodes answer; find the IPVS rules in the ingress sandbox (`nsenter --net=/run/docker/netns/ingress_sbox ipvsadm -Ln`).
- [ ] **42.4 Explain it** *(Level: Core)*
  - **Do:** a decision memo (≤ 10 lines) for a 4-person team running 6 services on 2 VMs, choosing one of the three, with your numbers from 42.1 and 42.2 and the decision tree of §17.

**Checkpoint (closed book):**
1. Which of the three reconcile desired state continuously?
2. How does Swarm load-balance a published port?
3. Give one reason to pick Swarm over Kubernetes, and one against.
<details><summary>Answers</summary>

1. Swarm and Kubernetes. Compose applies the file when you run it and relies on the Docker restart policy afterwards.
2. The routing mesh: every node listens on the published port, and IPVS in the ingress network namespace forwards to a task's VIP-backed backends on any node.
3. For: much less to operate for a small team on a few hosts, using the Compose format. Against: a far smaller ecosystem (operators, autoscalers, policy, managed offerings) and fewer extension points.
</details>

---

## 43 — Python Containers with uv: Performance and Cold Start  ([chapter](43-python-containers-with-uv-performance-and-cold-start.md))
**Time:** ~3 h · **Needs:** Docker with BuildKit, `uv`, kind `lab`, local `registry:2`

- [ ] **43.1 Naive vs the gold-standard Dockerfile** *(Level: Core)*
  - **Do:** a FastAPI app with ~15 dependencies. Image A: `FROM python:3.12`, `pip install -r requirements.txt`, `COPY . .`. Image B: the chapter's multi-stage `uv` build (§5, §19): lockfile first, `uv sync --locked --no-install-project --no-dev`, then the project, final stage slim or distroless.
  - **Predict:** sizes of A and B, and cold/warm build times.
  - **Verify:** a table: image → size → layers → cold build → rebuild after a source change.
- [ ] **43.2 The uv cache mount** *(Level: Core)*
  - **Do:** add one dependency with `uv add`; rebuild B with and without `--mount=type=cache,target=/root/.cache/uv` (and `UV_LINK_MODE=copy`) (§6).
  - **Predict:** the rebuild time with the mount.
  - **Verify:** both times; the cache is not in the final image.
- [ ] **43.3 Bytecode and import time** *(Level: Core)*
  - **Goal:** measure what `--compile-bytecode` buys (§8, §14, §16).
  - **Do:** build B with and without `UV_COMPILE_BYTECODE=1`. For each, run with `--read-only`: `python -X importtime -c "import app.main" 2> imp.log` and read the cumulative time on the `app.main` line; time container start → first 200 OK over 10 runs.
  - **Predict:** the import-time and first-response differences, and the size cost.
  - **Verify:** three numbers per variant; without bytecode on a read-only filesystem every start pays compilation.
- [ ] **43.4 Cold start in the cluster** *(Level: Core)*
  - **Do:** push A and B to the local registry wired into kind. For each: remove it from the node (`crictl rmi`), then time `kubectl run` → `kubectl wait --for=condition=Ready` with a readiness probe on `/health`, 5 runs (§18, §20).
  - **Predict:** how much of the difference is pull time.
  - **Verify:** median cold start for A and B, split into pull time (Pod events `Pulling` → `Pulled`) and startup time.
- [ ] **43.5 Memory per worker** *(Level: Stretch)*
  - **Do:** run B with 1, 2 and 4 uvicorn/gunicorn workers; read `/sys/fs/cgroup/memory.current` in the Pod after warm-up (§13, §15).
  - **Verify:** MiB per worker; set the memory limit from it with headroom and justify the number.

**Checkpoint (closed book):**
1. Why copy `pyproject.toml` and `uv.lock` before the source?
2. What does `--compile-bytecode` buy, and what does it cost?
3. What is the difference between `uv sync --locked` and `--frozen`?
<details><summary>Answers</summary>

1. The dependency install layer then depends only on the lockfile, so source edits reuse it from cache.
2. Faster first import (no compile at startup), which matters most on read-only filesystems where `.pyc` files cannot be written; it costs build time and image size.
3. `--locked` fails if the lockfile is out of date with `pyproject.toml`; `--frozen` uses the lockfile as is without checking it.
</details>

---

## 44 — Secrets and ConfigMaps Deep Dive  ([chapter](44-secrets-and-configmaps-deep-dive.md))
**Time:** ~3 h · **Needs:** kind `lab` (with `lab/extra/` mounted) · **First:** [k8s-learn/config-storage-tasks.md](../k8s-learn/config-storage-tasks.md) Levels 1–2, [env-config-secrets-tasks.md](../k8s-learn/env-config-secrets-tasks.md) Levels 2–3, 5

- [ ] **44.1 Watch the atomic swap** *(Level: Core)*
  - **Goal:** see the symlink tree of §4.2 change.
  - **Do:** mount a ConfigMap at `/etc/config` in an Alpine Pod with `inotify-tools`. `ls -la /etc/config`; run `inotifywait -m /etc/config`; update the ConfigMap.
  - **Predict:** the sequence of inotify events.
  - **Verify:** a new `..<timestamp>` directory is created, `..data_tmp` is created and renamed onto `..data`, and the old directory is deleted. The key files themselves are never written.
- [ ] **44.2 Propagation delay** *(Level: Core)*
  - **Do:** in the Pod, print the file's content with a timestamp every second. Update the ConfigMap 5 times and record the delay each time. Then update it and immediately add an annotation to the Pod (§5).
  - **Predict:** the maximum delay, from the kubelet's sync period and its ConfigMap change-detection strategy (`configz`).
  - **Verify:** 5 delays (typically up to about a minute); the annotation update triggers a Pod sync and a near-immediate refresh.
- [ ] **44.3 Encryption at rest, end to end** *(Level: Core, destructive)*
  - **Goal:** go past "base64 is not encryption" and actually encrypt (§2).
  - **Do:** create secret `old`. Write `lab/extra/enc.yaml`: an `EncryptionConfiguration` for `secrets` with `secretbox` (a random 32-byte base64 key) first and `identity` last. Add `--encryption-provider-config=/etc/kubernetes/extra/enc.yaml` to the apiserver manifest with `sed`. Create secret `new`. Dump both: `ketcd get /registry/secrets/default/<name> --print-value-only | head -c 48 | xxd`.
  - **Predict:** the first bytes of `old` and `new` in etcd.
  - **Verify:** `new` starts with `k8s:enc:secretbox:v1:key1:`; `old` is still plaintext protobuf until you rewrite it with `kubectl get secrets -A -o json | kubectl replace -f -`. Remove `identity` last and explain what breaks if you remove it first.
- [ ] **44.4 `immutable: true` and watch count** *(Level: Core)*
  - **Goal:** measure the control-plane saving of §7.
  - **Do:** 100 ConfigMaps, each mounted by one Pod. Read `kubectl get --raw /metrics | grep 'apiserver_longrunning_requests{.*resource="configmaps".*verb="WATCH"'`. Recreate the ConfigMaps with `immutable: true` and the Pods, and read it again.
  - **Predict:** the watch count in both runs.
  - **Verify:** about one watch per mounted ConfigMap with the default Watch strategy, far fewer when immutable. Try to edit an immutable ConfigMap and record the error.
- [ ] **44.5 External Secrets rotation** *(Level: Stretch)*
  - **Do:** install External Secrets Operator; a `SecretStore` with the `fake` provider; an `ExternalSecret` with `refreshInterval: 15s` mounted as a volume. Change the store's value and measure until the file in the Pod changes (§8, §9).
  - **Verify:** the end-to-end rotation delay = ESO refresh + kubelet propagation, both measured.

**Checkpoint (closed book):**
1. How does the kubelet update a mounted ConfigMap atomically?
2. Why does a `subPath` mount never see updates?
3. After you enable encryption at rest, are existing Secrets encrypted?
<details><summary>Answers</summary>

1. It writes a new timestamped directory, points a temporary `..data_tmp` symlink at it, and `rename(2)`s that over `..data`. Key files are symlinks through `..data`, so readers see the old or the new set, never a mix.
2. A `subPath` is bind-mounted once, at container start, to the file behind the symlinks. Later swaps change `..data`, not the mounted inode.
3. No. Only objects written after the change are encrypted. You must rewrite every Secret, then remove the `identity` provider.
</details>

---

## Capstone projects

Each takes 1–3 days on a laptop. Do them after the chapters they list. Write a short README per project with the measured numbers; that README is the deliverable.

### C1 — A hardened multi-tenant platform (chapters 06, 07, 20, 25, 27, 28)
- **Spec:** a `calico` kind cluster onboarding two tenants from one YAML bundle each: namespace with PSA `restricted`, group-bound RBAC, quota and LimitRange, default-deny network with a DNS allowance, a ValidatingAdmissionPolicy set (no `latest` tags, required resource requests, no `hostPath`), Kyverno signature verification for your registry prefix with digest mutation, Falco with one custom rule, and audit logging.
- **Acceptance:** a script runs 12 attack scenarios (privileged Pod, `hostPath`, unsigned image, retargeted tag, cross-tenant curl, cross-tenant `get secrets`, ClusterRole creation, quota exhaustion, shell in a container, `/etc/shadow` read, metadata-IP egress, impersonation) and prints blocked/detected/allowed for each. At most one "allowed", and it is documented.
- **What to measure:** p50/p99 Pod-create latency with all admission layers vs none; the number of warnings a real Helm chart (for example ingress-nginx) produces under your policies; the time from attack to Falco alert.

### C2 — Control-plane SLOs under fault injection (chapters 04, 05, 18, 22, 30, 35)
- **Spec:** kube-prometheus-stack on `lab` with recording rules and alerts for apiserver latency by verb, etcd fsync p99, scheduler attempt p99, CoreDNS latency, and workqueue depth; an app autoscaled by a custom metric (22.2). Inject five faults one at a time: a slow disk for etcd (`tc` or a cgroup `io.max` on the node), an APF-free LIST flood, CoreDNS scaled to 1 with `ndots:5` traffic, a controller hot loop, and 900 KiB ConfigMaps.
- **Acceptance:** each fault fires the right alert within 5 minutes, and a one-page runbook per fault names the dashboard panel, the metric, and the fix. The HPA keeps the app's p99 under a target you set in advance through the non-etcd faults.
- **What to measure:** time to detect per fault, SLI values during each fault, and time to recover after the fix.

### C3 — A backup operator with conversion and chaos (chapters 08, 13, 19, 23, 36)
- **Spec:** a `Backup` CRD (`v1alpha1` → `v1` with a conversion webhook) and controller-runtime operator that snapshots a StatefulSet's PVCs through CSI (hostpath driver), records `status.conditions` and `observedGeneration`, sets owner references on the `VolumeSnapshot`s, uses a finalizer to delete snapshots, and runs with leader election and 2 replicas.
- **Acceptance:** envtest suite passes; a chaos script kills the leader mid-backup 10 times and every backup ends `Succeeded` or `Failed`, never stuck; deleting a `Backup` removes its snapshots; a restore from any snapshot passes a data checksum.
- **What to measure:** backup duration vs data size, leader failover time, reconcile error rate, and workqueue retries during chaos.

### C4 — Disaster recovery drill with RTO and RPO (chapters 04, 31, 32, 44)
- **Spec:** everything in `lab` is deployed by Argo CD from a Git repo, Secrets come from External Secrets or are sealed, etcd is snapshotted every 5 minutes to MinIO, and stateful data is backed up by Velero. Run two drills: (a) etcd point-in-time restore after an accidental `kubectl delete ns` of a production namespace; (b) total cluster loss rebuilt from Git plus Velero on a fresh kind cluster.
- **Acceptance:** both drills end with the app serving correct data; a written runbook that someone else can follow; encryption-at-rest keys are backed up and the restore proves they work.
- **What to measure:** RTO and RPO for each drill against targets you write down before starting, and which step dominated each.
