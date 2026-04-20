"""
Pull-based failure detection.

How it works:
  - Opposite of push: followers PULL (poll) the leader for liveness.
  - Each follower periodically sends a "are you alive?" request to the leader.
  - If the leader doesn't respond within timeout -> suspect failure.
  - Used in some load balancer health checks and monitoring systems.

Pull vs Push trade-offs:
  - Push (heartbeat from leader):
      + Leader controls the rate
      + Followers are passive (less work)
      - If leader is overloaded, it may skip heartbeats -> false positive
  - Pull (followers poll leader):
      + Follower controls its own detection speed
      + Can include app-level health checks in the response
      - More load on the leader (must handle N poll requests)
      - All followers polling at once can cause thundering herd
"""

import random
import time
from typing import Self


class HealthStatus:
    """Response from a health check — can carry multi-level info."""
    def __init__(self, alive: bool, load: float = 0.0,
                 last_write_lag_ms: float = 0.0):
        self.alive: bool = alive
        self.load: float = load                  # CPU load 0.0-1.0
        self.last_write_lag_ms: float = last_write_lag_ms


class Node:
    def __init__(self, node_id: int, is_leader: bool = False):
        self.node_id: int = node_id
        self._is_leader: bool = is_leader
        self.is_alive: bool = True
        self._peers: list[Self] = []

        # Simulated node state
        self._cpu_load: float = 0.2
        self._write_lag_ms: float = 1.0

        # Failure detection state (followers track leader health)
        self._consecutive_failures: int = 0
        self._max_failures: int = 3     # declare dead after 3 failed polls
        self._leader_status: str = "HEALTHY"
        self._poll_results: list[str] = []  # history for display

    def set_peers(self, peers: list[Self]):
        self._peers = [p for p in peers if p.node_id != self.node_id]

    def get_leader(self) -> Self | None:
        for p in self._peers:
            if p._is_leader:
                return p
        return None

    # --- Leader side: respond to health check ---

    def handle_health_check(self) -> HealthStatus | None:
        """Leader responds to a health poll. Returns None if dead."""
        if not self.is_alive:
            return None  # no response (timeout on follower side)
        return HealthStatus(
            alive=True,
            load=self._cpu_load,
            last_write_lag_ms=self._write_lag_ms,
        )

    # --- Follower side: poll the leader ---

    def poll_leader(self) -> str:
        """
        Follower polls the leader. Implements multi-level health assessment:
          - Level 0: Is the leader process alive? (TCP connect)
          - Level 1: Can the leader respond to a health check? (app-level)
          - Level 2: Is the leader performing well? (latency/load check)
        """
        if self._is_leader:
            return "SKIP"

        leader = self.get_leader()
        if leader is None:
            return "NO_LEADER"

        response = leader.handle_health_check()

        # Level 0+1: No response at all -> hard failure
        if response is None:
            self._consecutive_failures += 1
            result = f"FAIL ({self._consecutive_failures}/{self._max_failures})"

            if self._consecutive_failures >= self._max_failures:
                self._leader_status = "DEAD"
                result = "DEAD — triggering election"
            else:
                self._leader_status = "SUSPECT"

            self._poll_results.append(result)
            return result

        # Got a response -> reset failure counter
        self._consecutive_failures = 0

        # Level 2: Check for gray failure (alive but degraded)
        if response.load > 0.9:
            self._leader_status = "DEGRADED"
            result = f"DEGRADED (CPU={response.load:.0%})"
        elif response.last_write_lag_ms > 500:
            self._leader_status = "DEGRADED"
            result = f"DEGRADED (write_lag={response.last_write_lag_ms:.0f}ms)"
        else:
            self._leader_status = "HEALTHY"
            result = (f"HEALTHY (CPU={response.load:.0%}, "
                      f"lag={response.last_write_lag_ms:.0f}ms)")

        self._poll_results.append(result)
        return result

    def crash(self):
        self.is_alive = False

    def degrade(self, cpu_load: float, write_lag_ms: float):
        """Simulate gray failure: node is alive but slow."""
        self._cpu_load = cpu_load
        self._write_lag_ms = write_lag_ms


# --- Simulation ---
if __name__ == "__main__":
    random.seed(99)

    # 4-node cluster: node 0 is leader, nodes 1-3 are followers
    leader = Node(node_id=0, is_leader=True)
    followers = [Node(node_id=i) for i in range(1, 4)]
    all_nodes = [leader] + followers

    for n in all_nodes:
        n.set_peers(all_nodes)

    # Scenario timeline:
    # Ticks 1-4:  Normal operation
    # Ticks 5-8:  Gray failure (leader overloaded)
    # Ticks 9-10: Recovery
    # Tick 11:    Hard crash
    # Ticks 12+:  Detection and election

    POLL_INTERVAL = 1.0

    print(f"{'='*70}")
    print("  Pull-based failure detection with multi-level health checks")
    print(f"{'='*70}")
    print(f"  Nodes: leader=N0, followers=N1,N2,N3")
    print(f"  Poll interval: {POLL_INTERVAL}s, "
          f"max failures before DEAD: {followers[0]._max_failures}")
    print(f"{'='*70}\n")

    for tick in range(1, 20):
        # Apply scenario events
        if tick == 5:
            print(f"  >>> INJECTING GRAY FAILURE: leader CPU=95%, "
                  f"write_lag=800ms <<<\n")
            leader.degrade(cpu_load=0.95, write_lag_ms=800)

        if tick == 9:
            print(f"  >>> LEADER RECOVERS: CPU=30%, write_lag=2ms <<<\n")
            leader.degrade(cpu_load=0.30, write_lag_ms=2.0)

        if tick == 11:
            print(f"  >>> LEADER HARD CRASH <<<\n")
            leader.crash()

        print(f"  --- tick {tick} ---")

        # Each follower polls the leader
        election_triggered = False
        for f in followers:
            result = f.poll_leader()
            print(f"    N{f.node_id} -> N0: {result}")
            if f._leader_status == "DEAD":
                election_triggered = True

        print()

        if election_triggered:
            print(f"{'='*70}")
            print(f"  Election triggered! All followers detected leader is dead.")
            print(f"{'='*70}\n")

            print("  Pull-based detection summary:")
            print(f"    - Gray failure (ticks 5-8): detected as DEGRADED")
            print(f"      (leader was alive but slow — no false failover)")
            print(f"    - Hard crash (tick 11): detected after "
                  f"{followers[0]._max_failures} consecutive poll failures")
            print(f"    - Total detection time: "
                  f"~{followers[0]._max_failures * POLL_INTERVAL:.0f}s "
                  f"after crash")
            break

        time.sleep(0.3)  # shorter sleep for demo
