import time
from typing import Self


class Node:
    def __init__(self, node_id: int, is_leader: bool = False):
        self._node_id: int = node_id
        self._is_leader: bool = is_leader
        self._is_alive: bool = True
        self._peers: list[Self] = []
        self._last_heartbeat_received: float = time.time()
        self._timeout_seconds: float = 5.0

    def set_peers(self, peers: list[Self]):
        self._peers = [p for p in peers if p._node_id != self._node_id]

    def send_heartbeats(self):
        """Leader pushes heartbeats to all followers."""
        if not self._is_leader:
            return
        for peer in self._peers:
            if peer._is_alive:
                peer.receive_heartbeat(self._node_id)

    def receive_heartbeat(self, from_node_id: int):
        """Follower receives a heartbeat from the leader."""
        self._last_heartbeat_received = time.time()

    def cron_live_check(self):
        """Follower checks if it has heard from the leader recently."""
        if self._is_leader:
            return
        elapsed = time.time() - self._last_heartbeat_received
        if elapsed > self._timeout_seconds:
            print(f"Node {self._node_id}: leader heartbeat timeout "
                  f"({elapsed:.1f}s > {self._timeout_seconds}s), "
                  f"triggering election")
            self._trigger_election()

    def _trigger_election(self):
        """Placeholder — in real systems this starts a Raft/ZAB election."""
        print(f"Node {self._node_id}: starting leader election")

    def crash(self):
        self._is_alive = False

    def __hash__(self):
        return self._node_id


# --- Simulation ---
if __name__ == "__main__":
    # Create a 3-node cluster: node 0 is leader, nodes 1 and 2 are followers
    leader = Node(node_id=0, is_leader=True)
    follower1 = Node(node_id=1)
    follower2 = Node(node_id=2)

    all_nodes = [leader, follower1, follower2]
    for node in all_nodes:
        node.set_peers(all_nodes)

    HEARTBEAT_INTERVAL = 1.0  # leader pings every 1s
    CHECK_INTERVAL = 1.0      # followers check every 1s
    CRASH_AT = 4               # leader crashes after tick 4

    tick = 0
    while True:
        tick += 1
        print(f"\n--- tick {tick} ---")

        # Phase 1: leader sends heartbeats (if alive)
        if leader._is_alive:
            leader.send_heartbeats()
            print(f"Node 0 (leader): heartbeat sent")

        # Phase 2: crash the leader at the designated tick
        if tick == CRASH_AT and leader._is_alive:
            leader.crash()
            print(f"Node 0 (leader): *** CRASHED ***")

        # Phase 3: followers run their liveness check
        election_triggered = False
        for node in all_nodes:
            if not node._is_leader:
                node.cron_live_check()
                if (time.time() - node._last_heartbeat_received
                        > node._timeout_seconds):
                    election_triggered = True

        if election_triggered:
            print("\nSimulation complete — election would start here.")
            break

        time.sleep(HEARTBEAT_INTERVAL)
