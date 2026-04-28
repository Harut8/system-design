# 10 — Alerting Strategy

> The fastest way to lose trust in a monitoring stack is to send the wrong alert to the wrong human at 3 AM. This doc lays out exactly which alerts page, which ticket, which inform — and why.

The economic claim: an alert that never causes action is **negative value**. It trains people to ignore the channel. So every alert here either has a runbook with a clear action or it doesn't ship.

---

## 1. Tier System

Three tiers. Match the *response time* to the consequence.

| Tier | Response | Channel | Repeat | Example |
|------|----------|---------|--------|---------|
| **P1 / page** | < 15 min | PagerDuty → human pager | every 1h | DBE detected; XID 79 |
| **P2 / ticket** | next business day | Jira / ServiceNow | once | Idle GPU 30m+; SBE rate climbing |
| **P3 / informational** | weekly review | Slack channel | digest | Allocation > 90%; RMA queue length |

Anything you'd page yourself for at 3 AM is P1. Anything you'd want to fix this week is P2. Anything that's just FYI is P3 — and don't dilute P1/P2 with FYI.

---

## 2. Hardware Alerts

These are platform-team-owned. They route to GPU infra on-call, never to workload teams.

### 2.1 Critical (page)

```yaml
- alert: GPU_DBE_Detected
  expr: increase(DCGM_FI_DEV_ECC_DBE_VOL_TOTAL[5m]) > 0
  for: 0s
  labels: { severity: critical, team: gpu-platform, tier: p1 }
  annotations:
    summary: "DBE on {{ $labels.gpu }} ({{ $labels.Hostname }})"
    runbook_url: https://wiki/runbooks/gpu-dbe
    description: |
      Uncorrectable memory error. Workload data may be corrupted.
      1. Cordon node
      2. Drain affected pods
      3. If recurring within 24h, RMA

- alert: GPU_FellOffBus
  expr: |
    increase(gpu_xid_total{code="79"}[1m]) > 0
    or DCGM_FI_DEV_XID_ERRORS == 79
  for: 0s
  labels: { severity: critical, team: gpu-platform, tier: p1 }
  annotations:
    summary: "GPU {{ $labels.gpu }} fell off bus on {{ $labels.Hostname }}"
    runbook_url: https://wiki/runbooks/gpu-xid79

- alert: GPU_FatalXID
  expr: |
    increase(gpu_xid_total{code=~"45|62|95"}[5m]) > 0
  for: 0s
  labels: { severity: critical, team: gpu-platform, tier: p1 }
  annotations:
    summary: "Fatal XID {{ $labels.code }} on {{ $labels.Hostname }}"
    runbook_url: https://wiki/runbooks/gpu-fatal-xid

- alert: GPU_HWThermalSlowdown
  expr: |
    (DCGM_FI_DEV_CLOCK_THROTTLE_REASONS & 0x40) > 0
  for: 5m
  labels: { severity: critical, team: gpu-platform, tier: p1 }
  annotations:
    summary: "Sustained HW thermal slowdown — {{ $labels.Hostname }} GPU {{ $labels.gpu }}"
    description: "Likely cooling failure. Drain workload."
    runbook_url: https://wiki/runbooks/gpu-thermal

- alert: GPU_NodeExporter_AllGonefor30s
  expr: absent_over_time(up{job="dcgm-exporter"}[5m])
  for: 0s
  labels: { severity: critical, team: gpu-platform, tier: p1 }
  annotations:
    summary: "All dcgm-exporters down — fleet observability lost"
```

### 2.2 Warning (ticket)

```yaml
- alert: GPU_SBE_RateClimbing
  expr: rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[1h]) > 10/3600
  for: 30m
  labels: { severity: warning, team: gpu-platform, tier: p2 }
  annotations:
    summary: "SBE rate climbing on {{ $labels.gpu }} ({{ $labels.Hostname }})"

- alert: GPU_PageRetirement_Approaching
  expr: DCGM_FI_DEV_RETIRED_DBE > 32
  for: 1m
  labels: { severity: warning, team: gpu-platform, tier: p2 }
  annotations:
    summary: "{{ $value }} pages retired on {{ $labels.gpu }} (RMA threshold = 64)"

- alert: GPU_NVLinkRecovery_Climbing
  expr: rate(DCGM_FI_DEV_NVLINK_RECOVERY_ERROR_COUNT_TOTAL[1h]) > 0
  for: 1h
  labels: { severity: warning, team: gpu-platform, tier: p2 }
  annotations:
    summary: "NVLink fabric degrading — link {{ $labels.nvlink }} on {{ $labels.Hostname }}"

- alert: GPU_PCIe_LinkDegraded
  expr: DCGM_FI_DEV_PCIE_LINK_GEN < 5 or DCGM_FI_DEV_PCIE_LINK_WIDTH < 16
  for: 5m
  labels: { severity: warning, team: gpu-platform, tier: p2 }
  annotations:
    summary: "PCIe link degraded on {{ $labels.Hostname }} GPU {{ $labels.gpu }}"

- alert: GPU_TempSustained
  expr: avg_over_time(DCGM_FI_DEV_GPU_TEMP[10m]) > 85
  for: 10m
  labels: { severity: warning, team: gpu-platform, tier: p2 }

- alert: GPU_HBM_TempSustained
  expr: avg_over_time(DCGM_FI_DEV_MEMORY_TEMP[10m]) > 93
  for: 10m
  labels: { severity: warning, team: gpu-platform, tier: p2 }

- alert: GPU_ProfilingLockStolen
  expr: |
    absent_over_time(DCGM_FI_PROF_SM_ACTIVE[5m])
    and on(Hostname) up{job="dcgm-exporter"} == 1
  for: 5m
  labels: { severity: warning, team: gpu-platform, tier: p2 }
  annotations:
    summary: "Profiling lock stolen on {{ $labels.Hostname }} — Nsight or vendor agent?"
```

---

## 3. Utilization & Allocation Alerts

These route to ML platform team or namespace owners.

### 3.1 Idle GPU (ticket)

```yaml
- alert: GPU_Idle_Allocated
  expr: |
    avg_over_time(DCGM_FI_PROF_SM_ACTIVE[30m]) < 0.05
    and on(pod, namespace) group_left() (
      sum by (pod, namespace) (
        kube_pod_container_resource_requests{resource="nvidia_com_gpu"}
      ) > 0
    )
  for: 30m
  labels: { severity: warning, team: ml-platform, tier: p2 }
  annotations:
    summary: "GPU allocated but idle: pod {{ $labels.namespace }}/{{ $labels.pod }}"

- alert: Notebook_Idle_Reclaim_Imminent
  expr: |
    avg_over_time(DCGM_FI_PROF_SM_ACTIVE[2h]) < 0.02
    and on(pod, namespace) group_left() kube_pod_labels{label_app="jupyterhub"}
  for: 2h
  labels: { severity: info, team: ml-platform, tier: p3 }
  annotations:
    summary: "Notebook idle 2h+, will reclaim — {{ $labels.namespace }}/{{ $labels.pod }}"
```

### 3.2 Capacity warnings

```yaml
- alert: Cluster_GPU_AllocationHigh
  expr: |
    sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})
    /
    sum(kube_node_status_allocatable{resource="nvidia_com_gpu"})
    > 0.95
  for: 15m
  labels: { severity: warning, team: gpu-platform, tier: p2 }
  annotations:
    summary: "GPU allocation > 95% — capacity wall approaching"

- alert: GPU_Pod_PendingTooLong
  expr: |
    (time() - kube_pod_created)
    * on(pod, namespace) group_left() kube_pod_status_phase{phase="Pending"} == 1
    * on(pod, namespace) group_left() (sum by (pod, namespace) (
        kube_pod_container_resource_requests{resource="nvidia_com_gpu"} > 0
      ))
    > 600   # 10 min
  for: 5m
  labels: { severity: warning, team: gpu-platform, tier: p2 }
  annotations:
    summary: "GPU pod pending > 10m: {{ $labels.namespace }}/{{ $labels.pod }}"
```

---

## 4. Inference SLO Alerts

Route to the **service owner**, not platform.

```yaml
- alert: Inference_p99_Latency
  expr: |
    histogram_quantile(0.99,
      sum by (service, le) (rate(inference_request_duration_seconds_bucket[5m]))
    ) > 0.5
  for: 5m
  labels: { severity: critical, team_route: "{{ $labels.service }}-oncall", tier: p1 }
  annotations:
    summary: "p99 latency > 500ms on {{ $labels.service }}"
    runbook_url: https://wiki/runbooks/inference-latency

- alert: Inference_TTFT_Slow
  expr: |
    histogram_quantile(0.95,
      sum by (service, le) (rate(vllm:time_to_first_token_seconds_bucket[5m]))
    ) > 1.0
  for: 5m
  labels: { severity: warning, team_route: "{{ $labels.service }}-oncall", tier: p2 }

- alert: Inference_TokenThroughput_Drop
  expr: |
    (
      avg_over_time(rate(vllm:generation_tokens_total[5m])[1h:5m])
      - rate(vllm:generation_tokens_total[5m])
    )
    /
    avg_over_time(rate(vllm:generation_tokens_total[5m])[1h:5m])
    > 0.20
  for: 10m
  labels: { severity: warning, team_route: "{{ $labels.service }}-oncall", tier: p2 }
  annotations:
    summary: "Token throughput dropped >20% on {{ $labels.service }}"

- alert: Inference_KVCache_Saturating
  expr: max(vllm:kv_cache_usage_perc) > 0.95
  for: 5m
  labels: { severity: warning, team_route: "{{ $labels.service }}-oncall", tier: p2 }
  annotations:
    summary: "KV cache > 95% on {{ $labels.service }} — preemption imminent"

- alert: Inference_QueueBacklog
  expr: vllm:num_requests_waiting > 50
  for: 5m
  labels: { severity: warning, team_route: "{{ $labels.service }}-oncall", tier: p2 }
```

---

## 5. Training Job Alerts

Routed to the model owner / training-team.

```yaml
- alert: Training_StragglerDetected
  expr: |
    (
      max by (job_id) (
        histogram_quantile(0.5, sum by (job_id, rank, le) (
          rate(train_step_seconds_bucket[5m])
        ))
      )
      /
      quantile by (job_id) (0.5,
        histogram_quantile(0.5, sum by (job_id, rank, le) (
          rate(train_step_seconds_bucket[5m])
        ))
      )
    ) > 1.10
  for: 10m
  labels: { severity: warning, team_route: "{{ $labels.team }}", tier: p2 }
  annotations:
    summary: "Straggler in {{ $labels.job_id }} — slowest rank > 10% slower than median"

- alert: Training_StepTimeRegression
  expr: |
    histogram_quantile(0.95, rate(train_step_seconds_bucket[5m]))
    > 1.5 * histogram_quantile(0.95, rate(train_step_seconds_bucket[5m] offset 1h))
  for: 15m
  labels: { severity: warning, team_route: "{{ $labels.team }}", tier: p2 }

- alert: Training_GPU_Utilization_Low
  expr: |
    avg by (job_id) (
      DCGM_FI_PROF_SM_ACTIVE
      * on(pod, namespace) group_left(job_id) kube_pod_labels
    ) < 0.30
  for: 30m
  labels: { severity: info, team_route: "{{ $labels.team }}", tier: p3 }
  annotations:
    summary: "Job {{ $labels.job_id }} averaging < 30% SM_active for 30m"
```

---

## 6. Routing — `alertmanager.yml`

The full routing tree, label-driven:

```yaml
route:
  receiver: default-slack
  group_by: [alertname, cluster]
  group_wait: 10s
  group_interval: 5m
  repeat_interval: 4h

  routes:
    # Critical → page
    - match: { tier: p1 }
      receiver: pagerduty-gpu-platform
      group_wait: 0s
      repeat_interval: 1h
      continue: true   # also send to slack
      routes:
        - match: { team_route: vllm-llm-oncall }
          receiver: pagerduty-llm-team

    # Warning → ticket via Jira
    - match: { tier: p2 }
      receiver: jira-ticket
      group_wait: 30s
      repeat_interval: 24h

    # Info → digest
    - match: { tier: p3 }
      receiver: slack-digest
      group_interval: 1h
      repeat_interval: 24h

receivers:
  - name: pagerduty-gpu-platform
    pagerduty_configs:
      - service_key: $PAGERDUTY_GPU_PLATFORM_KEY
        severity: '{{ .CommonLabels.severity }}'
        details:
          alertname: '{{ .GroupLabels.alertname }}'
          runbook: '{{ .CommonAnnotations.runbook_url }}'

  - name: pagerduty-llm-team
    pagerduty_configs:
      - service_key: $PAGERDUTY_LLM_KEY

  - name: jira-ticket
    webhook_configs:
      - url: http://jira-bridge:8080/alert
        send_resolved: true

  - name: slack-digest
    slack_configs:
      - api_url: $SLACK_DIGEST_URL
        channel: '#gpu-fleet-info'

  - name: default-slack
    slack_configs:
      - api_url: $SLACK_DEFAULT_URL
        channel: '#gpu-alerts'
```

---

## 7. Inhibition Rules

The single most-important inhibition is "if a node is being drained, suppress all per-pod alerts on that node." Otherwise an evacuating node generates dozens of false positives.

```yaml
inhibit_rules:
  # If node is being drained, suppress utilization alerts
  - source_matchers:
      - alertname = NodeDraining
    target_matchers:
      - alertname =~ GPU_Idle.*|Inference_.*|Training_.*
    equal: [Hostname]

  # If exporter is down, suppress per-GPU alerts (we can't see them anyway)
  - source_matchers:
      - alertname = GPU_NodeExporter_AllGonefor30s
    target_matchers:
      - alertname =~ GPU_.*
    equal: [Hostname]

  # DBE supersedes SBE warning on the same GPU
  - source_matchers:
      - alertname = GPU_DBE_Detected
    target_matchers:
      - alertname = GPU_SBE_RateClimbing
    equal: [Hostname, gpu]

  # XID 79 supersedes all per-GPU warnings
  - source_matchers:
      - alertname = GPU_FellOffBus
    target_matchers:
      - alertname =~ GPU_.*
    equal: [Hostname, gpu]

  # Cluster-wide capacity alerts: don't ticket idle GPU when cluster is full
  - source_matchers:
      - alertname = Cluster_GPU_AllocationHigh
    target_matchers:
      - alertname = GPU_Idle_Allocated
    equal: [cluster]
```

---

## 8. Runbooks: the alert-annotation contract

Every alert with severity ≥ warning must have a `runbook_url` annotation. Runbooks live in `/wiki/runbooks/<alertname>` and follow a fixed template:

```markdown
# Runbook: GPU_DBE_Detected

## Summary
Double-bit ECC error has been detected on a GPU. Memory is corrupted.

## Severity
P1 / Critical / Page

## What does this mean?
The GPU's HBM memory hardware has detected an uncorrectable bit-flip.
Whatever workload was running may have produced incorrect output.

## Impact
- Workload running on this GPU should be considered tainted
- The GPU may continue to generate further DBEs
- Hardware is degrading

## Actions (in order)
1. Cordon the node: `kubectl cordon <node>`
2. Identify affected pod: query DCGM_FI_DEV_GPU_TEMP{...,gpu="X"} with pod label
3. Drain pod: `kubectl delete pod <pod>`
4. Open RMA ticket if 2nd DBE within 24h

## When NOT to act
- If the node is already in `state=in_rma`, this alert is expected
- If a planned drain is in progress, suppress with maintenance silence

## Escalation
- L1 platform → L2 hardware → vendor RMA
```

The cost of writing the runbook is small. The cost of not having one is the on-call playing detective at 2 AM.

---

## 9. Silences and Maintenance

Three silence patterns:

| Pattern | When | Duration |
|---------|------|----------|
| Node maintenance | Drain + reboot | 2h |
| Cluster maintenance | Upgrade | 4h |
| Per-alert | Known false positive | open until cause fixed |

Silences should be created via API with a clear `comment` field and an expiration. A silence without expiration is a long-term suppression that should become a deletion of the alert.

```bash
amtool silence add \
  Hostname=gpu-014 \
  --duration=2h \
  --author=harut \
  --comment="planned drain for thermal paste replacement"
```

---

## 10. Alert Hygiene — Quarterly Review

Two queries to run quarterly to keep the alert set healthy:

```promql
# Alerts that fire often but never get resolved by action
sum by (alertname) (
  ALERTS{alertstate="firing"}
)

# Alerts that have never fired (candidates for deletion)
absent_over_time(ALERTS_FOR_STATE{alertname="X"}[90d])
```

Plus an action review: for each P1 page in the last quarter, was the action taken what the runbook said? If runbook drift is high, fix the runbooks.

---

## 11. Synthetic Tests

Don't ship alerts you haven't tested. The minimum:

- **DBE injection**: `dcgmi diag -r 3` triggers DCGM diagnostic mode that surfaces errors. Used to verify the DBE alert path.
- **XID 79 simulation**: harder to inject; use a pre-prod GPU and physically remove power for 1s, observe alert fires.
- **Profiling lock**: run `nsys profile` on a node, confirm `GPU_ProfilingLockStolen` fires within 5m.
- **Idle GPU**: schedule a pod that requests a GPU but only sleeps. Verify `GPU_Idle_Allocated` fires.

A monthly synthetic-tests CI run catches alert regressions before they hide a real failure.

---

## 12. Acceptance Checklist

- [ ] All P1 alerts have runbooks linked
- [ ] All P1 alerts route to PagerDuty with correct receiver
- [ ] P2 alerts open Jira tickets, not pages
- [ ] P3 alerts go to a digest channel, not the main alerts channel
- [ ] Inhibition rules in place (drain, exporter-down, hierarchy)
- [ ] Synthetic tests for DBE, XID 79, profiling lock pass
- [ ] Silences have expirations
- [ ] Quarterly hygiene review scheduled

---

## 13. Forward Pointers

- **Doc 13**: alert routing per tenant in multi-tenant clusters
- **Doc 14**: LLM-specific alerts (TTFT, KV cache, queue depth)
- **Doc 15**: distributed training alerts (NCCL hang, straggler chains)

---

## Sources

- [Prometheus — Alertmanager Configuration](https://prometheus.io/docs/alerting/latest/configuration/)
- [PagerDuty Prometheus Integration Guide](https://www.pagerduty.com/docs/guides/prometheus-integration-guide/)
- [Robust Perception — PagerDuty + Alertmanager](https://www.robustperception.io/using-pagerduty-with-the-alertmanager/)
- [Last9 — Prometheus Alertmanager](https://last9.io/blog/prometheus-alertmanager/)
- [DataOps Tech — Routing alerts by severity](https://medium.com/dataops-tech/routing-alerts-in-slack-pagerduty-by-severity-so-noise-doesnt-kill-you-874060ef2996)
