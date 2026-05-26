Here is the complete, comprehensive master plan for transitioning **IoT_Graduate** from a centralized architecture to a decentralized Peer-to-Peer (P2P) network, embedding Pareto Optimization ($\epsilon$-constraint method) and a Room-less WebRTC Grid Monitor.

---

## Phase 1: Network Architecture Shift (Centralized to P2P Pure-LAN)

The central Master Node and its singular orchestrator process are completely eliminated. Every Edge Node (NVIDIA Jetson) is upgraded to a **Peer Node** that manages its own local DeepStream pipeline while concurrently participating in cluster-wide load balancing.

### 1. Unified Message Bus Layout

The local MQTT broker remains on the local network as a shared, high-speed message bus, but communication transitions to a mesh-broadcast paradigm.

### 2. Decentralized MQTT Topic Topology

* `peers/status/<node_id>`: Every Peer broadcasts a heartbeat every 2 seconds containing its real-time hardware metrics (CPU, GPU, RAM, temperature) and an array of its currently active camera IDs.
* `peers/vote/request`: Broadcast by an overloaded Peer to publish a Request for Offload (RFO) for a specific camera stream.
* `peers/vote/proposal`: Eligible Peers publish their bidding packages (proposals) to this topic.
* `peers/vote/decision`: The final consensus output is published here to declare the election winner and trigger migration.

---

## Phase 2: Core Offloading Logic (Pareto Optimization via $\epsilon$-Constraint)

To balance cluster loads optimally, migrating a camera is treated as a **Multi-Objective Optimization Problem** to satisfy competing system demands: minimizing node load, maximizing AI frame rates, minimizing network latency, and preventing migration churn.

### 1. Mathematical Formulation using the $\epsilon$-Constraint Method

To keep computations lightweight for embedded Jetson hardware, the system minimizes a primary objective function while converting all other objectives into strict boundary constraints ($\epsilon$):

$$\text{Minimize: } F(x) = \text{Estimated Resource Load Score of the target Peer after migration}$$

$$\text{Subject to the following local constraints:}$$

* **Performance Constraint ($\epsilon_{fps}$):** The bidding Peer’s predicted pipeline performance must guarantee $FPS_{predicted} \ge 18$ to preserve traffic analysis integrity.
* **Network Constraint ($\epsilon_{network}$):** The network round-trip delay from the bidding Peer to the target camera's RTSP origin must satisfy $Latency \le 50\text{ms}$.
* **Stability Constraint ($\epsilon_{cooldown}$):** To eliminate thrashing (the ping-pong effect where a stream bounces endlessly between two nodes), the time since the Peer's last migration must be $\ge 45\text{s}$.
* **Capacity Constraint ($\epsilon_{streams}$):** Total concurrent streams assigned to the bidding Peer must not exceed hardware limits ($\le 4$ streams).

### 2. Dynamic Boundary Adaptation (Dynamic $\epsilon$-Tuning)

During peak traffic hours, a strict $\epsilon$ policy might result in a "zero-bid" election where no peer qualifies. The system implements a cascaded relaxation loop:

* *Tier 1 Relaxation:* Increase $\epsilon_{network}$ to $80\text{ms}$, expanding the spatial boundaries for acceptable video streaming.
* *Tier 2 Relaxation:* Degrading $\epsilon_{fps}$ from $18$ down to $15$ or $12$ FPS. This trades off brief video fluidity to lower the thermal and compute load of an endangered node.

---

## Phase 3: Local Peer Discovery (LAN Topology Mapping)

To execute voting rounds, each Jetson must maintain an accurate registry of its active neighborhood ("comrades") on the LAN without relying on a central database.

```
[Node Startup] ── Step 1 ──> Read Edge/configs/edge_node.yml (Static IP Fallback Registry)
               ── Step 2 ──> Broadcast UDP mDNS packet to dynamically announce/discover active Peers

```

1. **Extended Local Configuration:** The pre-existing local node configuration file (`Edge/configs/edge_node.yml`) is expanded to include a `lan_topology` section. This block serves as a static fallback registry, cataloging peer hardware identifiers, their hardcoded LAN IPs, and stream capacities.
2. **Dynamic mDNS (Multicast DNS) Beaconing:** For true zero-configuration deployments, each Peer spins up an mDNS worker broadcasting a custom UDP service string (`_iot_graduate._tcp`). When a new Jetson joins the switch, it announces its presence; neighboring nodes listen to these beacons and update their local state registries automatically.

---

## Phase 4: Room-less WebRTC & Dynamic Grid Monitor Dashboard

The legacy virtual "rooms" architecture (`?room=name`) and the separate `signaling_server.py` process are completely decommissioned. Video streams are aggregated directly via ID routing into a consolidated matrix monitor.

### 1. ID-Based WebRTC Signaling Routing

* **Concept:** WebRTC connection metadata negotiation (SDP Offers, Answers, and ICE Candidates) bypasses web sockets entirely and routes over the shared MQTT bus.
* **Topic Scheme:** `peers/webrtc/signaling/<camera_id>/+`. DeepStream processing pipelines map their outbound WebRTC media tracks directly to their unique `camera_id` token.

### 2. Autonomous Grid Monitor Mechanics

The display terminal (Grid Monitor) operates as a headless WebRTC client that listens to the cluster configuration dynamically:

* **Auto-Grid Scaling Layout:** The monitor subscribes to `peers/status/+`. By parsing the concurrent heartbeats, it builds a map of active camera IDs and dynamically sizes an HTML5 CSS Grid container (e.g., a $2 \times 2$ grid for 4 active cameras; a $3 \times 3$ layout for 6 streams).
* **Seamless WebRTC Hot-Swapping:** Integration with the Make-before-Break protocol guarantees visual continuity:
1. When `Peer_A` nhường (cedes) `cam_01` to `Peer_B` after a Pareto election, the Monitor detects the ownership shift via the public MQTT status channel.
2. The Monitor initializes a background WebRTC connection handshake with `Peer_B` (**Make** phase) while the active video pane continues rendering the feed from `Peer_A`.
3. Once the 10-second time lease expires, the connection to `Peer_A` is dropped (**Break** phase). The view switches to `Peer_B` instantly with zero display flickering, black screen frames, or page reloads.



---

## Phase 5: Leaderless Fail-Over (Cluster Self-Healing)

If an Edge Node encounters an immediate hardware fault or loses power, the cluster executes recovery without any central coordinator:

1. **Mesh Timeout Verification:** Alive Peers watch the `peers/status/+` heartbeat stream. If a node fails to report a status update for longer than 15 seconds, it is marked as `OFFLINE`.
2. **Deterministic Hash Mapping:** All surviving nodes independently feed the orphaned camera IDs and the sorted list of surviving peer IDs into a shared mathematical hashing function.
3. **Silent Assignment:** Because the inputs are identical and the hashing function is deterministic, every peer calculates the exact same target node assignment (e.g., all nodes independently calculate that `Peer_C` is responsible for rescuing `cam_01`). The chosen node (`Peer_C`) silently appends the stream to its local pipeline. No network negotiation or master intervention is required.