# Service & Networking Tasks — Services, Endpoints, DNS, Ingress

Track A module 5. Do after `config-storage-tasks.md`.
Read alongside: `../kubernetes/14-services-and-kube-proxy.md`, `../kubernetes/18-dns-and-coredns.md`.

> The one idea: **a Service is not a proxy. It is a label selector plus a virtual
> IP that every node's dataplane rewrites.** There is no process listening on a
> ClusterIP. Nothing is "running" the Service. It is a set of packet-rewriting
> rules derived from a list of Pod IPs.
>
> ```
>   Service (selector) ──▶ EndpointSlice (real Pod IPs, only Ready ones)
>                              │
>                              ▼
>          kube-proxy writes iptables/IPVS rules on EVERY node
>                              │
>   client → ClusterIP:80 ──DNAT──▶ 10.244.1.7:8080
> ```

Setup: `kubectl create ns net-lab`. Assume `-n net-lab` throughout.

---

## Level 0 — Orientation

1. `kubectl explain service.spec` — note `selector`, `ports`, `type`, `clusterIP`.
2. Two backing objects: `kubectl get endpoints,endpointslices -n net-lab`
3. Know your CIDRs — Pod IPs and Service IPs come from different pools:
   ```bash
   kubectl cluster-info dump | grep -m2 -E 'service-cluster-ip-range|cluster-cidr'
   ```
4. Groundwork:
   ```bash
   kubectl create deploy web -n net-lab --image=nginx --replicas=3
   kubectl expose deploy web -n net-lab --port=80 --target-port=80
   ```

---

## Level 1 — Service and EndpointSlice

- [ ] **Task 1.1 — The selector is the whole mechanism**
  ```bash
  kubectl get svc web -n net-lab -o jsonpath='{.spec.selector}{"\n"}'
  kubectl get endpointslices -n net-lab -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.endpoints[*].addresses}{"\n"}{end}'
  kubectl get pods -n net-lab -o wide
  ```
  - Verify: the EndpointSlice addresses are exactly the Pod IPs.
  - Learn: the endpoints controller watches Pods matching the selector and
    maintains the slice. **A Service with a typo'd selector is a perfectly healthy
    object with zero endpoints** — no error anywhere.

- [ ] **Task 1.2 — Break it and see the failure mode**
  - Do: `kubectl patch svc web -n net-lab -p '{"spec":{"selector":{"app":"nope"}}}'`
  - Verify: `kubectl get endpointslices -n net-lab` — empty. Curl the ClusterIP →
    connection refused / timeout.
  - Learn: **"no endpoints" is the #1 Service bug** and it always means the
    selector doesn't match Pod labels. Revert with the original selector.

- [ ] **Task 1.3 — Only Ready pods get endpoints**
  - Do: add a failing readinessProbe to one replica, or
    `kubectl patch` the deployment with a probe pointing at a closed port.
  - Verify: that Pod's IP disappears from the EndpointSlice while the Pod stays
    Running.
  - Learn: **readiness controls traffic, liveness controls restarts.** This is why
    readiness probes matter — they are the only way a Pod removes itself from a
    Service without dying.

- [ ] **Task 1.4 — targetPort vs port**
  - Learn: `port` is the Service's port; `targetPort` is the container's. Naming
    the container port and referring to it by name (`targetPort: http`) survives
    the container port changing.

- [ ] **Task 1.5 — There is no process on a ClusterIP**
  - Do: `kubectl get svc web -n net-lab -o jsonpath='{.spec.clusterIP}{"\n"}'`, then
    on a node: `sudo iptables-save | grep <clusterIP>` (kind:
    `docker exec net-lab-control-plane iptables-save | grep <ip>`)
  - Verify: DNAT rules, one per endpoint, with statistic-based probability.
  - Learn: load balancing is **random per-connection**, done in the kernel on the
    *client's* node. No hop through a central proxy. Which also means: no
    connection draining, no retries, no L7 awareness.

---

## Level 2 — Service types

- [ ] **Task 2.1 — ClusterIP (default)**
  - Do: `kubectl run tmp -n net-lab --rm -it --image=nicolaka/netshoot -- curl -s web`
  - Learn: reachable only inside the cluster.

- [ ] **Task 2.2 — Headless**
  ```bash
  kubectl create svc clusterip web-headless -n net-lab --clusterip="None" --tcp=80:80
  kubectl patch svc web-headless -n net-lab -p '{"spec":{"selector":{"app":"web"}}}'
  kubectl run tmp -n net-lab --rm -it --image=nicolaka/netshoot -- dig +short web-headless.net-lab.svc.cluster.local
  ```
  - Verify: **all Pod IPs returned**, not one virtual IP.
  - Learn: no ClusterIP, no iptables rules — DNS returns the backends directly and
    the client chooses. This is what StatefulSets use for stable per-Pod identity.

- [ ] **Task 2.3 — NodePort**
  - Do: `kubectl patch svc web -n net-lab -p '{"spec":{"type":"NodePort"}}'`
  - Verify: `kubectl get svc web -n net-lab` shows `80:3xxxx/TCP`. That port is open
    on **every** node, whether or not it runs a Pod.
  - Learn: default range 30000–32767. Every NodePort is cluster-wide — they're a
    finite, global namespace, which is why they don't scale as an ingress strategy.

- [ ] **Task 2.4 — LoadBalancer**
  - Do: patch to `LoadBalancer` on kind.
  - Verify: `EXTERNAL-IP` stays `<pending>` forever — no cloud controller.
  - Learn: `LoadBalancer` is a **superset** of NodePort, and it asks a cloud
    controller to provision a real LB. No controller, no IP. This is exactly why
    people install MetalLB on bare metal.

- [ ] **Task 2.5 — ExternalName**
  ```bash
  kubectl create svc externalname db -n net-lab --external-name=db.example.com
  ```
  - Learn: pure DNS CNAME, no proxying, no endpoints. Useful for migrating an
    external dependency behind a stable in-cluster name.

---

## Level 3 — DNS

- [ ] **Task 3.1 — The name hierarchy**
  ```bash
  kubectl run tmp -n net-lab --rm -it --image=nicolaka/netshoot -- sh
  # inside:
  cat /etc/resolv.conf
  nslookup web
  nslookup web.net-lab
  nslookup web.net-lab.svc.cluster.local
  ```
  - Learn: `<svc>.<ns>.svc.cluster.local` is the FQDN. Short names work via the
    `search` list in `resolv.conf`, which is namespace-scoped — so `web` resolves
    differently depending on which namespace you're in.

- [ ] **Task 3.2 — The ndots:5 tax**
  - Do: note `options ndots:5` in `/etc/resolv.conf`.
  - Learn: any name with fewer than 5 dots is tried against **every** search domain
    first. `api.github.com` (2 dots) generates 4 failed lookups before the real
    one. At scale this is a genuine CoreDNS load problem, and it's why you'll see
    `api.github.com.` (trailing dot) in production configs.

- [ ] **Task 3.3 — SRV records for named ports**
  - Do: `dig +short SRV _http._tcp.web.net-lab.svc.cluster.local` (name the port
    `http` first).
  - Learn: this is how clients discover ports without hardcoding them.

- [ ] **Task 3.4 — Headless DNS gives per-Pod names**
  - Learn: with a headless Service and a StatefulSet, each Pod gets
    `<pod>.<svc>.<ns>.svc.cluster.local` — stable, resolvable, and the basis of
    every clustered database's peer discovery.

- [ ] **Task 3.5 — dnsPolicy**
  - Learn: `ClusterFirst` (default) → CoreDNS. `Default` → inherit the node's
    resolver (**note the confusing name — it is not the default**).
    `None` + `dnsConfig` → full control. `hostNetwork` Pods silently get `Default`
    unless you set `ClusterFirstWithHostNet`, which breaks cluster DNS for them.

---

## Level 4 — Traffic policy and topology

- [ ] **Task 4.1 — externalTrafficPolicy**
  - Do: on the NodePort Service, set `externalTrafficPolicy: Local`.
  - Learn:
    - `Cluster` (default): any node accepts and may forward to another node.
      Extra hop, and the **source IP is SNAT'd away**.
    - `Local`: only nodes running a Pod accept. Source IP preserved, no extra hop,
      but load is uneven if Pods aren't spread evenly.
  - **Rule:** if you need real client IPs, you need `Local` (or a proxy that sets
    `X-Forwarded-For`).

- [ ] **Task 4.2 — internalTrafficPolicy**
  - Learn: `Local` keeps in-cluster traffic on the originating node. Cuts
    cross-AZ cost — and silently blackholes traffic if the node has no local Pod.

- [ ] **Task 4.3 — Session affinity**
  - Do: `sessionAffinity: ClientIP`
  - Learn: iptables-level, source-IP based, with a timeout. It is **not** cookie
    affinity and it breaks behind any NAT that collapses many clients to one IP.

- [ ] **Task 4.4 — Topology-aware routing**
  - Learn: the `service.kubernetes.io/topology-mode: Auto` annotation makes
    EndpointSlices zone-aware so traffic prefers same-zone endpoints. Real money
    on cross-AZ egress. It disables itself when endpoints are unbalanced — which
    is confusing until you know it's deliberate.

---

## Level 5 — Ingress and beyond

- [ ] **Task 5.1 — Install an ingress controller**
  ```bash
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
  ```
  - Learn: an Ingress **object** does nothing on its own. A controller watches
    Ingress objects and configures a real proxy. No controller = no effect, and no
    error either.

- [ ] **Task 5.2 — Route by host and path**
  ```yaml
  apiVersion: networking.k8s.io/v1
  kind: Ingress
  metadata: {name: web, namespace: net-lab}
  spec:
    ingressClassName: nginx
    rules:
    - host: web.local
      http:
        paths:
        - path: /
          pathType: Prefix
          backend: {service: {name: web, port: {number: 80}}}
  ```
  - Do: `curl -H 'Host: web.local' localhost`
  - Learn: L7. One IP fronting many services, TLS termination, host/path routing —
    all the things a Service cannot do.

- [ ] **Task 5.3 — pathType matters**
  - Learn: `Prefix` matches on **path segments** (`/api` matches `/api/v1`, not
    `/apis`). `Exact` is literal. `ImplementationSpecific` means the controller
    decides — usually regex, and usually where the surprises are.

- [ ] **Task 5.4 — Gateway API**
  - Do: `kubectl get crd | grep gateway.networking.k8s.io`
  - Learn: the successor. Splits the one overloaded Ingress object into
    `GatewayClass` (infra), `Gateway` (listener, platform team) and `HTTPRoute`
    (routing, app team) — a real RBAC boundary that Ingress never had. New work
    should target it; see `../kubernetes/17-ingress-gateway-and-service-mesh.md`.

---

## Level 6 — Edge Cases & Production Nuances

### EC-1 — Service exists, endpoints empty

- **Diagnose, in order:**
  ```bash
  kubectl get endpointslices -n NS -l kubernetes.io/service-name=SVC
  kubectl get svc SVC -n NS -o jsonpath='{.spec.selector}{"\n"}'
  kubectl get pods -n NS --show-labels
  kubectl get pods -n NS -o wide     # are they Ready?
  ```
- **Causes, in frequency order:** selector/label mismatch · Pods not Ready ·
  wrong namespace · `targetPort` doesn't match a listening port.
- **Rule:** always check endpoints before debugging anything else. It splits the
  problem cleanly into "the Service isn't finding Pods" versus "the network is
  broken," and those have nothing in common.

---

### EC-2 — Connections break during a rolling update

- **Trap:** 502s during deploys despite a readiness probe.
- **Why:** a race. The Pod gets `SIGTERM` and stops accepting *before* kube-proxy
  on every node has removed its rule. In-flight connections hit a closing Pod.
- **Fix:** a `preStop` sleep so the Pod stays up while endpoints propagate:
  ```yaml
  lifecycle: {preStop: {exec: {command: ["sh","-c","sleep 5"]}}}
  ```
  plus `terminationGracePeriodSeconds` comfortably above it.
- **Rule:** endpoint removal is **eventually consistent across every node**. Graceful
  shutdown must outlast propagation. This is the most common cause of deploy-time
  errors in real clusters.

---

### EC-3 — DNS caching hides the truth

- **Trap:** you fix a Service and the client still fails for minutes.
- **Why:** the JVM caches DNS forever by default (`networkaddress.cache.ttl`),
  Go's resolver doesn't cache but connection pools do, and NodeLocal DNSCache adds
  another layer.
- **Rule:** when testing DNS, use a fresh `netshoot` pod. Never trust a
  long-running application's view.

---

### EC-4 — ndots:5 melts CoreDNS

- **Trap:** CoreDNS at high CPU, latency everywhere, no obvious cause.
- **Diagnose:** `kubectl logs -n kube-system -l k8s-app=kube-dns` — a flood of
  NXDOMAIN for names like `api.github.com.net-lab.svc.cluster.local`.
- **Fix:** trailing dots on external names, or per-Pod `dnsConfig: {options: [{name: ndots, value: "1"}]}`.
- **Rule:** every external hostname costs 4–5 lookups by default (Task 3.2).

---

### EC-5 — `hostNetwork` breaks cluster DNS

- **Trap:** a `hostNetwork: true` Pod can't resolve Service names.
- **Why:** it gets `dnsPolicy: Default` (the node's resolver), not `ClusterFirst`.
- **Fix:** `dnsPolicy: ClusterFirstWithHostNet`.
- **Rule:** the `Default` policy is not the default policy. Genuinely bad naming
  that costs everyone an afternoon once.

---

### EC-6 — NodePort source IP disappears

- **Trap:** all requests appear to come from a node IP.
- **Why:** `externalTrafficPolicy: Cluster` SNATs so the return path works
  (Task 4.1).
- **Fix:** `Local`, accepting the uneven-load tradeoff, or terminate at an L7 proxy
  and read `X-Forwarded-For`.

---

### EC-7 — Two Services selecting the same Pods

- **Trap:** unpredictable routing, or a Pod receiving traffic it shouldn't.
- **Why:** nothing prevents overlapping selectors. A Pod can be in any number of
  Services.
- **Rule:** selectors are not exclusive and there is no ownership. Be specific with
  labels; `app: web` alone will eventually collide.

---

### EC-8 — Headless Service with no ready pods returns NXDOMAIN

- **Trap:** startup deadlock in a StatefulSet — peers can't resolve each other
  because none are Ready, and none become Ready because they can't find peers.
- **Fix:** `publishNotReadyAddresses: true` on the headless Service.
- **Rule:** this is mandatory for most clustered databases and it's why StatefulSet
  examples always include it.

---

## Cheat sheet

```bash
kubectl expose deploy D --port=80 --target-port=8080
kubectl get endpointslices -n NS -l kubernetes.io/service-name=SVC
kubectl get svc SVC -o jsonpath='{.spec.selector}{"\n"}'
kubectl get pods --show-labels
kubectl run tmp --rm -it --image=nicolaka/netshoot -- sh   # dig, curl, tcpdump, ss
dig +short SVC.NS.svc.cluster.local
dig +short SRV _http._tcp.SVC.NS.svc.cluster.local
kubectl port-forward svc/SVC 8080:80                        # bypasses kube-proxy
docker exec <kind-node> iptables-save | grep <clusterIP>
kubectl logs -n kube-system -l k8s-app=kube-dns
```

## Mental model to lock in

- **A Service is a selector plus rules, not a process.** Nothing listens on a
  ClusterIP; every node's kernel rewrites packets.
- **Endpoints are the truth.** Empty endpoints = selector or readiness problem,
  never a network problem. Check them first, always.
- **Readiness controls traffic; liveness controls restarts.** Different jobs.
- **Endpoint changes are eventually consistent across all nodes** — hence `preStop`
  sleeps and 502s during deploys.
- **Headless = DNS returns Pods.** ClusterIP = DNS returns one virtual IP.
- **`ndots:5` makes every external name cost ~5 lookups.**
- **Ingress objects do nothing without a controller**, and fail silently without one.

```text
  selector ──▶ EndpointSlice ──▶ kube-proxy (every node) ──▶ iptables/IPVS
     │              ▲                                            │
     │              │ only Ready pods                            │ DNAT
     ▼              │                                            ▼
   Service ─────── Pods                                    ClusterIP:port
     │                                                      → podIP:targetPort
     ├── ClusterIP     internal VIP
     ├── Headless      no VIP, DNS → all pod IPs
     ├── NodePort      + every node:3xxxx
     ├── LoadBalancer  + cloud LB (needs a controller)
     └── ExternalName  CNAME only, no endpoints

   Ingress / Gateway ── L7 ──▶ Service ──▶ Pods
```
