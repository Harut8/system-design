"""
Gossip / SWIM-based failure detection.

How it works (used by Consul, Serf, ScyllaDB):
  - No leader. Every node is equal.
  - Each tick, a node picks ONE random peer and sends a PING.
  - If no ACK within timeout -> ask K random peers to INDIRECT PROBE.
  - If indirect probes also fail -> mark node as SUSPECT.
  - Suspicion is disseminated via gossip piggyback on protocol messages.
  - After suspicion timeout with no refutation -> declare DEAD.

Key properties:
  - O(1) messages per node per tick (scalable to 1000s of nodes)
  - Indirect probes detect asymmetric network partitions
  - Suspicion period avoids false positives from transient failures
"""

import random
import time
from enum import Enum
from typing import Self


class Status(Enum):
    ALIVE = "ALIVE"
    SUSPECT = "SUSPECT"
    DEAD = "DEAD"


class Node:
    def __init__(self, node_id: int):
        self.node_id: int = node_id
        self.is_alive: bool = True  # physical state (can this node respond?)
        self._peers: list[Self] = []

        # Membership table: what this node BELIEVES about every other node
        self._member_status: dict[int, Status] = {}
        self._suspect_since: dict[int, float] = {}

        self._suspicion_timeout: float = 5.0
        self._indirect_probe_count: int = 2  # K in SWIM paper

    def set_peers(self, peers: list[Self]):
        self._peers = [p for p in peers if p.node_id != self.node_id]
        for p in self._peers:
            self._member_status[p.node_id] = Status.ALIVE

    # --- Network simulation helpers ---

    def _send_ping(self, target: Self) -> bool:
        """Simulate sending a PING. Returns True if ACK received."""
        if not target.is_alive:
            return False
        # In a real system this is a UDP packet + timeout
        return True

    def _request_indirect_probe(self, prober: Self, target: Self) -> bool:
        """Ask 'prober' to ping 'target' on our behalf."""
        if not prober.is_alive:
            return False
        return self._send_ping(target)

    # --- SWIM protocol round ---

    def swim_tick(self):
        """One SWIM protocol period: pick a random peer, probe it."""
        if not self.is_alive:
            return

        # Step 1: pick a random peer to probe
        target = random.choice(self._peers)

        # Step 2: direct ping
        ack = self._send_ping(target)
        if ack:
            self._mark_alive(target.node_id)
            return

        # Step 3: direct ping failed -> indirect probes
        other_peers = [p for p in self._peers
                       if p.node_id != target.node_id and p.is_alive]
        probers = random.sample(
            other_peers,
            min(self._indirect_probe_count, len(other_peers))
        )

        indirect_ack = False
        for prober in probers:
            if self._request_indirect_probe(prober, target):
                indirect_ack = True
                break

        if indirect_ack:
            # Target is reachable through another path (asymmetric issue)
            self._mark_alive(target.node_id)
            print(f"  Node {self.node_id}: indirect probe to "
                  f"Node {target.node_id} succeeded (asymmetric link?)")
        else:
            # All probes failed -> suspect
            self._mark_suspect(target.node_id)

    def _mark_alive(self, peer_id: int):
        if self._member_status.get(peer_id) == Status.SUSPECT:
            print(f"  Node {self.node_id}: Node {peer_id} "
                  f"SUSPECT -> ALIVE (refuted)")
        self._member_status[peer_id] = Status.ALIVE
        self._suspect_since.pop(peer_id, None)

    def _mark_suspect(self, peer_id: int):
        if self._member_status.get(peer_id) != Status.SUSPECT:
            print(f"  Node {self.node_id}: Node {peer_id} "
                  f"marked SUSPECT (ping + indirect probes failed)")
            self._suspect_since[peer_id] = time.time()
        self._member_status[peer_id] = Status.SUSPECT

    def _mark_dead(self, peer_id: int):
        print(f"  Node {self.node_id}: Node {peer_id} "
              f"declared DEAD (suspicion timeout expired)")
        self._member_status[peer_id] = Status.DEAD
        self._suspect_since.pop(peer_id, None)

    def expire_suspects(self):
        """Promote suspects to DEAD after suspicion timeout."""
        if not self.is_alive:
            return
        now = time.time()
        for peer_id, since in list(self._suspect_since.items()):
            if now - since > self._suspicion_timeout:
                self._mark_dead(peer_id)

    # --- Gossip dissemination ---

    def gossip_merge(self, other: Self):
        """
        Merge membership views with another node (piggybacked on SWIM msgs).
        In real SWIM this is piggy-backed on PING/ACK, not a separate step.
        """
        if not self.is_alive or not other.is_alive:
            return
        for peer_id, status in other._member_status.items():
            if peer_id == self.node_id:
                continue
            my_status = self._member_status.get(peer_id, Status.ALIVE)
            # Propagate worse status: ALIVE < SUSPECT < DEAD
            rank = {Status.ALIVE: 0, Status.SUSPECT: 1, Status.DEAD: 2}
            if rank[status] > rank[my_status]:
                self._member_status[peer_id] = status
                if status == Status.SUSPECT and peer_id not in self._suspect_since:
                    self._suspect_since[peer_id] = time.time()

    def status_summary(self) -> str:
        parts = []
        for pid in sorted(self._member_status):
            parts.append(f"N{pid}={self._member_status[pid].value}")
        return f"Node {self.node_id}: [{', '.join(parts)}]"

    def crash(self):
        self.is_alive = False


# --- Simulation ---
if __name__ == "__main__":
    random.seed(42)

    NUM_NODES = 6
    CRASH_NODE = 3
    CRASH_AT_TICK = 4
    SUSPICION_TIMEOUT = 3.0

    nodes = [Node(i) for i in range(NUM_NODES)]
    for n in nodes:
        n._suspicion_timeout = SUSPICION_TIMEOUT
        n.set_peers(nodes)

    tick = 0
    all_confirmed_dead = False

    while not all_confirmed_dead:
        tick += 1
        print(f"\n{'='*60}")
        print(f"  TICK {tick}")
        print(f"{'='*60}")

        # Crash the target node
        if tick == CRASH_AT_TICK and nodes[CRASH_NODE].is_alive:
            nodes[CRASH_NODE].crash()
            print(f"\n  *** Node {CRASH_NODE} CRASHED ***\n")

        # Phase 1: each alive node runs one SWIM probe
        for n in nodes:
            n.swim_tick()

        # Phase 2: gossip — each alive node exchanges state with 1 random peer
        for n in nodes:
            if not n.is_alive:
                continue
            partner = random.choice([p for p in nodes
                                     if p.node_id != n.node_id and p.is_alive])
            n.gossip_merge(partner)
            partner.gossip_merge(n)

        # Phase 3: expire suspects -> DEAD
        for n in nodes:
            n.expire_suspects()

        # Print cluster view from each alive node
        print()
        for n in nodes:
            if n.is_alive:
                print(f"  {n.status_summary()}")

        # Check if all alive nodes agree the crashed node is DEAD
        alive_nodes = [n for n in nodes if n.is_alive]
        all_confirmed_dead = all(
            n._member_status.get(CRASH_NODE) == Status.DEAD
            for n in alive_nodes
        )

        time.sleep(0.5)  # shorter sleep for demo

    print(f"\n{'='*60}")
    print(f"  All nodes agree: Node {CRASH_NODE} is DEAD "
          f"(detected in {tick - CRASH_AT_TICK} ticks after crash)")
    print(f"{'='*60}")
