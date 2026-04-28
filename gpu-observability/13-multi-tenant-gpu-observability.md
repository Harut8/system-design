# 13 — Multi-Tenant GPU Observability

> When multiple teams share the cluster, observability has to enforce isolation as well as deliver insight. Tenants must see their own data clearly; they must not see anyone else's; and noisy-neighbor problems must surface to platform operators without exposing the noisy neighbor's identity to the affected tenant.

This is a policy doc as much as a technical one. The technical pieces (RBAC, label scoping, query filtering) are easy. The hard part is what to share and what not to.

---

## 1. Isolation Levels — What's Already in Place

Recall from doc 03 §6 the GPU sharing modes. They have very different observability characteristics:

| Mode | Per-tenant DCGM | Per-tenant memory | Cross-tenant interference |
|------|------------------|-------------------|---------------------------|
| **Whole GPU** | Yes | Yes | None |
| **MIG** | Yes | Yes | None |
| **MPS** | Conflated | Shared | Possible |
| **Time-sliced** | Conflated | Shared | High |

For a multi-tenant production cluster, **whole-GPU or MIG are the only acceptable modes for tenants who don't trust each other**. MPS and time-slicing are fine within a single team or for non-confidential workloads.

---

## 2. Tenant Boundary: namespace-based

The pragmatic boundary: each tenant owns one or more namespaces. RBAC, network policies, resource quotas, and observability scoping all key off `namespace`.

The metrics dimension that maps to tenancy:

```promql
DCGM_FI_PROF_SM_ACTIVE{namespace="team-a"}
```

If the metric has `namespace`, it's filterable per tenant. If it doesn't (like `DCGM_FI_DEV_GPU_TEMP` for a dedicated GPU), the metric is platform-scope and not exposed to tenants.

### 2.1 Tenant-scoped views

The split:

| Metric category | Visibility | Reason |
|----------------|-----------|--------|
| Pod-scoped (has `namespace`) | Tenant + Platform | Tenant's own workload |
| Node-scoped (no `namespace`) | Platform only | Hardware health is platform's job |
| Cluster-aggregate | Platform only (or shared) | Cross-tenant reveals other tenants |

Tenants get a Grafana dashboard showing their workloads' GPU metrics. Platform sees the union plus the hardware view.

---

## 3. Label-Based Tenant Filtering in Mimir

Mimir's killer feature for multi-tenant: per-tenant scoping enforced server-side. Configured via `X-Scope-OrgID` header on read/write requests.

```yaml
# In Mimir's overrides config
overrides:
  team-a:
    max_global_series_per_user: 5_000_000
    max_query_length: 30d
    ingestion_rate: 50000

  team-b:
    max_global_series_per_user: 1_000_000
    max_query_length: 7d
    ingestion_rate: 10000
```

A query against tenant `team-a` only sees series ingested with `X-Scope-OrgID: team-a`. Cross-tenant query is structurally impossible — Mimir doesn't return them.

### 3.1 The two-tenant model

Most production clusters end up with this pattern:

- **Per-team tenants** (`team-a`, `team-b`, ...): receive only their own pod-scoped metrics
- **Platform tenant** (`platform`): receives everything, used for the platform's own dashboards

When tenant `team-a` opens Grafana, they see only `team-a` data.

### 3.2 Routing to per-tenant tenancy

In Prometheus remote_write:

```yaml
remote_write:
  - url: https://mimir/api/v1/push
    headers:
      X-Scope-OrgID: platform   # everything goes to platform tenant
  - url: https://mimir/api/v1/push
    headers:
      X-Scope-OrgID: team-a
    write_relabel_configs:
      - source_labels: [namespace]
        regex: "team-a-.+"
        action: keep
```

That writes platform-tenant gets *all* data, plus a separate write per team that only includes their namespace's series.

The trade-off: storage is doubled (data lives twice in Mimir) but query separation is hard. For most production stacks, this is the right call.

---

## 4. Grafana RBAC

Grafana 10+ supports per-organization datasources and per-folder permissions. The model:

- One Grafana **org** per top-level group (e.g., `engineering`, `research`)
- Within an org, **folders** per team
- Each folder pinned to a Mimir datasource configured for that tenant's `X-Scope-OrgID`

```jsonnet
// Per-team folder
local team_folder(team) = g.folder.new(team)
  + g.folder.withPermissions({
      role: 'Viewer',
      user: '{{ .Tenant }}',
      team: team,
    });

// Per-team datasource
local team_datasource(team) = g.datasource.prometheus.new('Mimir-' + team)
  + g.datasource.prometheus.withUrl('http://mimir-querier:8080/prometheus')
  + g.datasource.prometheus.withCustomHttpHeaders({
      'X-Scope-OrgID': team,
    });
```

Tenants can build dashboards in their folder using their datasource. They cannot escape into other folders or other datasources. The platform team has admin access across all orgs.

### 4.1 Shared / read-only dashboards

The L1 fleet dashboard (doc 09) is platform-only. The platform might *also* publish a "shared cluster utilization" dashboard that tenants can view, with platform-scope data filtered down to non-sensitive aggregates:

```promql
# Aggregate only — no per-tenant breakdown visible
avg(cluster:gpu_sm_active:avg1m)
```

Don't expose `team:` rollups here. Tenants can see their own (in their folder); they can't see other teams' aggregate.

---

## 5. Quota Visibility

Each tenant should see their own quota status:

```promql
# Per-tenant quota usage
sum by (namespace) (kube_pod_container_resource_requests{
  resource="nvidia_com_gpu",
  namespace="$namespace"
})
/
sum by (namespace) (kube_resourcequota{
  resource="requests.nvidia.com/gpu",
  type="hard",
  namespace="$namespace"
})
```

Plus alerts on their own breaches:

```yaml
- alert: Tenant_QuotaApproachingExhausted
  expr: |
    sum by (namespace) (kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
    /
    sum by (namespace) (kube_resourcequota{resource="requests.nvidia.com/gpu", type="hard"})
    > 0.9
  for: 5m
  labels: { severity: warning, team_route: "{{ $labels.namespace }}-oncall" }
```

The alert routes to the tenant's own on-call (using a `team_route` label), not the platform team.

---

## 6. Noisy Neighbor Detection

The hardest multi-tenant problem: tenant A's workload is degrading tenant B's workload, and B sees the symptom but can't see the cause (which is A's pod, which they shouldn't be told about).

Sources of cross-tenant interference:

| Source | Sharing mode that exposes it | How to detect |
|--------|------------------------------|---------------|
| HBM bandwidth contention | MPS / time-slicing | `DRAM_active` saturated while one tenant's `SM_active` low |
| L2 cache thrash | MPS / time-slicing | per-kernel cache hit rate (profiling) |
| PCIe contention | Same physical GPU | PCIe bandwidth saturated while one tenant copying |
| NVLink contention | Multi-GPU pods on shared baseboard | Aggregate NVLink saturation, per-pod metric impossible |
| Power throttling shared by node | Always | All tenants slow simultaneously when throttle bit set |
| NUMA contention | Multi-tenant node | CPU pressure on the same NUMA |

### 6.1 The detection query (platform-side)

```promql
# Symptom: one tenant's SM_active dropped while another's stayed high on the same GPU
(
  avg by (UUID, namespace) (DCGM_FI_PROF_SM_ACTIVE)
  - avg by (UUID, namespace) (DCGM_FI_PROF_SM_ACTIVE offset 30m)
) < -0.3
  and on(UUID) count by (UUID) (avg by (UUID, namespace) (DCGM_FI_PROF_SM_ACTIVE)) > 1
```

Translates to: this GPU's SM_active for *this* namespace dropped by 30%, *and* multiple namespaces share this GPU. Suggests interference.

The platform team gets the paging alert. The affected tenant gets an info-only "your performance dropped, possible noisy neighbor — platform investigating" notification *without* the offender's identity.

---

## 7. Cost Attribution per Tenant

The cost picture per tenant ties back to doc 5. The sensitive part: each tenant should see their own number, not others'.

Tenant-scoped recording rule (per tenant in Mimir):

```yaml
- record: tenant:gpu_cost_dollars:1h
  expr: |
    sum by (namespace) (
      kube_pod_container_resource_requests{resource="nvidia_com_gpu", namespace="$tenant"}
    ) * 4.20
```

Tenant sees *their* number. Platform sees the union. CFO sees the rollup.

---

## 8. Privacy Considerations

The metric stack leaks identity in subtle ways. Things to scrub:

| Leak | Fix |
|------|-----|
| Pod names embedding user names | Don't put `user` in pod names; use opaque IDs |
| Custom labels with PII (`label_email=...`) | Drop in relabel; alert if seen |
| Job IDs that encode customer names | Use UUIDs, not customer-named jobs |
| Histogram buckets revealing rare workloads | Keep buckets coarse |

A scan over your label values is worth running periodically:

```promql
# Top 50 unique values per common label
topk(50, count by (pod) ({__name__=~".+"}))
topk(50, count by (label_team) (kube_pod_labels))
```

Find anything that looks personal, escalate.

---

## 9. Tenant SLOs

Each tenant gets an SLO contract with the platform. The standard ones:

| SLO | Definition | How measured |
|-----|-----------|--------------|
| Allocation availability | Tenant can schedule X GPUs within Y minutes | `kube_pod_status_phase{phase="Pending"}` p95 wait time |
| Hardware availability | Allocated GPUs are healthy | DBE rate, throttle rate |
| Performance variance | A given workload completes in stable time | Step time stddev for training |
| Idle reclaim notice | Tenant gets X minutes warning | `notebook_idle_seconds_remaining` |

SLO breach reporting goes to the tenant + platform; a quarterly SLO review surfaces the worst-affected tenants.

---

## 10. Multi-Tenant Cardinality Limits

Doc 8 covered cardinality globally. In multi-tenant Mimir, cardinality is per-tenant:

```yaml
overrides:
  team-a:
    max_global_series_per_user: 5_000_000
  team-large-fleet:
    max_global_series_per_user: 50_000_000
```

A misbehaving tenant can blow their own budget without affecting others. The alert:

```yaml
- alert: Tenant_CardinalityHigh
  expr: |
    cortex_ingester_active_series{user!="platform"} / 1000000 > 4
  for: 30m
  labels: { severity: warning, team_route: "{{ $labels.user }}-oncall" }
```

Routed to the tenant team (they have to fix it), with a link to doc 8 in the runbook.

---

## 11. The Per-Tenant Dashboard Template

What every tenant gets out of the box:

| Panel | Source |
|-------|--------|
| Team scoreboard (just their team) | tenant-scoped recording rules |
| GPU utilization per pod | DCGM_FI_PROF_SM_ACTIVE filtered to tenant ns |
| Quota usage | kube_resourcequota |
| Pending pods | kube_pod_status_phase |
| Their cost (last 30d) | tenant:gpu_cost_dollars rollup |
| Their workloads with low utilization | for self-improvement |

The platform builds this once as a template; each tenant gets a copy in their folder, datasource pre-pinned to their Mimir tenant.

---

## 12. Multi-Tenant Sharing Mode Decision Per Cluster

Different clusters can have different sharing policies:

| Cluster | Mode | Tenants |
|---------|------|---------|
| `prod-llm-inference` | MIG (1g/3g) | Customer-facing serving |
| `prod-training` | Whole GPU | Internal training jobs |
| `dev-shared` | Time-slicing or MPS | Notebooks, dev-loop |
| `research` | Whole GPU + spot | Research bursts |

The observability stack respects this: dev-shared accepts that per-tenant DCGM metrics will be conflated; prod clusters guarantee per-tenant fidelity.

---

## 13. Acceptance Checklist

- [ ] Mimir tenants configured for each major team + platform
- [ ] Grafana orgs/folders per team, datasource pinned
- [ ] Tenant cannot query cross-tenant data (verified by trying)
- [ ] Per-tenant cardinality limits in Mimir
- [ ] Per-tenant quota usage dashboard
- [ ] Noisy-neighbor detection alert exists
- [ ] Privacy scan for PII in labels documented
- [ ] Tenant SLO definitions written

---

## 14. Forward Pointers

- **Doc 14**: LLM serving — special case where multi-tenant is per-LoRA-adapter
- **Doc 15**: training — usually single-tenant per cluster, simpler

---

## Sources

- [Kubernetes — Multi-tenancy](https://kubernetes.io/docs/concepts/security/multi-tenancy/)
- [Northflank — Kubernetes Multi-Tenancy 2026 Guide](https://northflank.com/blog/kubernetes-multi-tenancy)
- [vCluster — Isolating Workloads in a Multi-Tenant GPU Cluster](https://www.vcluster.com/blog/isolating-workloads-multitenant-gpu-cluster)
- [Spectro Cloud — Noisy neighbor in multi-tenant Kubernetes](https://www.spectrocloud.com/blog/managing-the-noisy-neighbor-problem-in-kubernetes-multi-tenancy)
- [Grafana Mimir — Multi-tenancy](https://grafana.com/docs/mimir/latest/configure/about-tenant-ids/)
