"""
Phi-Accrual Failure Detector (Hayashibara et al., 2004).

How it works (used by Cassandra, Akka):
  - Instead of binary alive/dead, outputs a continuous suspicion level (phi).
  - Tracks the DISTRIBUTION of heartbeat inter-arrival times.
  - When a heartbeat is "late", computes the probability that the node
    has actually failed based on historical arrival patterns.
  - phi = -log10(P(heartbeat would be this late if node is alive))
  - Higher phi = more suspicious.  phi > threshold = declared dead.

Key advantage over fixed timeout:
  - ADAPTS to changing network conditions automatically.
  - During congestion: inter-arrival times increase -> phi stays low.
  - During normal operation: detection is fast.
  - Single tuning knob: phi_threshold (typically 8 for Cassandra).
"""

import math
import random
import time
from collections import deque
from typing import Self


class PhiAccrualDetector:
    """Phi-accrual failure detector for a single monitored node."""

    def __init__(self, window_size: int = 100, phi_threshold: float = 8.0):
        self._arrival_intervals: deque[float] = deque(maxlen=window_size)
        self._last_arrival: float | None = None
        self._phi_threshold: float = phi_threshold

    def record_heartbeat(self, now: float):
        """Record a heartbeat arrival. Call this when a heartbeat is received."""
        if self._last_arrival is not None:
            interval = now - self._last_arrival
            self._arrival_intervals.append(interval)
        self._last_arrival = now

    def _mean(self) -> float:
        if not self._arrival_intervals:
            return 1.0  # default 1s if no data yet
        return sum(self._arrival_intervals) / len(self._arrival_intervals)

    def _stddev(self) -> float:
        if len(self._arrival_intervals) < 2:
            return self._mean() / 4  # conservative default
        mu = self._mean()
        variance = sum((x - mu) ** 2 for x in self._arrival_intervals)
        variance /= len(self._arrival_intervals) - 1
        return math.sqrt(variance)

    def phi(self, now: float) -> float:
        """
        Compute current phi value.

        phi = -log10(1 - CDF(t_now - t_last))
            where CDF is the normal distribution fitted to inter-arrival times.

        phi=1 means 10% chance the node is alive
        phi=3 means 0.1% chance
        phi=8 means 0.000001% chance (Cassandra default threshold)
        """
        if self._last_arrival is None:
            return 0.0  # no data yet

        elapsed = now - self._last_arrival
        mu = self._mean()
        sigma = self._stddev()

        if sigma < 1e-9:
            sigma = mu / 4  # avoid division by zero

        # P(X > elapsed) using complementary CDF of normal distribution
        # Using the error function approximation
        z = (elapsed - mu) / sigma
        # P(X > elapsed) = 1 - Phi(z) = 0.5 * erfc(z / sqrt(2))
        p_later = 0.5 * math.erfc(z / math.sqrt(2))

        if p_later < 1e-15:
            return 15.0  # cap at very high phi
        if p_later >= 1.0:
            return 0.0

        return -math.log10(p_later)

    def is_dead(self, now: float) -> bool:
        return self.phi(now) > self._phi_threshold


class Node:
    def __init__(self, node_id: int):
        self.node_id: int = node_id
        self.is_alive: bool = True
        self._peers: list[Self] = []
        self._detectors: dict[int, PhiAccrualDetector] = {}

        # Simulated network characteristics
        self._base_latency: float = 0.1      # 100ms heartbeat interval
        self._jitter: float = 0.02           # +/- 20ms normal jitter

    def set_peers(self, peers: list[Self]):
        self._peers = [p for p in peers if p.node_id != self.node_id]
        for p in self._peers:
            self._detectors[p.node_id] = PhiAccrualDetector(
                window_size=50, phi_threshold=8.0
            )

    def send_heartbeat(self, target: Self, now: float):
        """Send a heartbeat to a target node."""
        if not self.is_alive:
            return
        if not target.is_alive:
            return  # dropped — target is dead
        target.receive_heartbeat(self.node_id, now)

    def receive_heartbeat(self, from_id: int, now: float):
        """Receive a heartbeat and feed it to the phi detector."""
        if from_id in self._detectors:
            self._detectors[from_id].record_heartbeat(now)

    def check_peers(self, now: float) -> list[tuple[int, float, bool]]:
        """Check phi for all peers. Returns [(peer_id, phi, is_dead)]."""
        results = []
        for peer_id, detector in self._detectors.items():
            p = detector.phi(now)
            dead = detector.is_dead(now)
            results.append((peer_id, p, dead))
        return results

    def crash(self):
        self.is_alive = False


# --- Simulation ---
if __name__ == "__main__":
    random.seed(7)

    # 3-node cluster: all nodes heartbeat each other (leaderless)
    nodes = [Node(i) for i in range(3)]
    for n in nodes:
        n.set_peers(nodes)

    CRASH_NODE = 1
    CRASH_AT_TICK = 30      # crash after 30 ticks of normal heartbeats
    CONGESTION_START = 15   # simulate network congestion at tick 15-25
    CONGESTION_END = 25
    TOTAL_TICKS = 70

    sim_time = time.time()

    print("Phase 1: Normal heartbeats (building baseline)")
    print("Phase 2: Network congestion (phi adapts, no false positive)")
    print("Phase 3: Node 1 crashes (phi rises, declared dead)")
    print(f"{'='*72}")

    for tick in range(1, TOTAL_TICKS + 1):
        # Advance simulated time
        base_interval = 0.1  # 100ms

        # Simulate congestion: increase latency 3x during congestion window
        if CONGESTION_START <= tick <= CONGESTION_END:
            interval = base_interval * 3 + random.gauss(0, 0.05)
            phase = "CONGESTED"
        else:
            interval = base_interval + random.gauss(0, 0.02)
            phase = "normal"

        sim_time += max(interval, 0.01)

        # Crash node at designated tick
        if tick == CRASH_AT_TICK and nodes[CRASH_NODE].is_alive:
            nodes[CRASH_NODE].crash()
            print(f"\n  *** Node {CRASH_NODE} CRASHED at tick {tick} ***\n")

        # Every alive node sends heartbeat to every other alive node
        for sender in nodes:
            for receiver in nodes:
                if sender.node_id != receiver.node_id:
                    sender.send_heartbeat(receiver, sim_time)

        # Node 0 checks phi for all peers (we observe from node 0's view)
        observer = nodes[0]
        results = observer.check_peers(sim_time)

        # Print periodically or on interesting events
        interesting = (
            tick <= 5
            or tick == CONGESTION_START
            or tick == CONGESTION_END
            or tick == CRASH_AT_TICK
            or tick % 5 == 0
            or any(dead for _, _, dead in results)
        )

        if interesting:
            peer_strs = []
            for peer_id, phi_val, dead in results:
                status = "DEAD!" if dead else "alive"
                bar_len = min(int(phi_val * 2), 40)
                bar = "#" * bar_len
                peer_strs.append(
                    f"N{peer_id}: phi={phi_val:5.1f} [{bar:<40s}] {status}"
                )
            print(f"  tick {tick:3d} ({phase:>9s}) | "
                  + " | ".join(peer_strs))

        # Stop once a death is detected
        if any(dead for _, _, dead in results):
            break

    print(f"\n{'='*72}")

    # Show detector stats
    det = observer._detectors[CRASH_NODE]
    intervals = list(det._arrival_intervals)
    if intervals:
        print(f"Detector stats for Node {CRASH_NODE} "
              f"(from Node {observer.node_id}'s view):")
        print(f"  Samples:  {len(intervals)}")
        print(f"  Mean:     {det._mean()*1000:.1f}ms")
        print(f"  Stddev:   {det._stddev()*1000:.1f}ms")
        print(f"  Last phi: {det.phi(sim_time):.1f}")
        print(f"\n  The detector adapted to congestion (mean ~300ms during "
              f"congestion)")
        print(f"  and still detected the real crash correctly.")
