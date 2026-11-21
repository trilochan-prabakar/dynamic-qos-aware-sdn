#!/usr/bin/env python3
"""
qos_controller.py
------------------------------------------------------------------
Ryu controller implementing "Dynamic QoS-Aware Path Selection".

Pipeline (matches the Methodology slide of the project):

    1. Collect network statistics       -> _monitor() + port stats reply
    2. Measure QoS metrics              -> _update_link_cost()
    3. Calculate QoS score              -> _link_score()
    4. Compare available paths          -> _best_path()  (networkx)
    5. Choose best path                 -> install_path()
    6. Update flow tables               -> OFPFlowMod per hop
    7. Monitor again / re-optimize      -> loop, re-route active flows

Design notes
------------
* The current Mininet topology is a four-switch diamond: host-facing
  switches s1 and s4, with paths s1-s2-s4 and s1-s3-s4. Static QoS
  profiles mirror the configured link characteristics in topology.py.
* Topology (switches + links) is auto-discovered via
  ryu.topology.api / ryu.topology.switches, so this also works with
  other topologies as long as QOS_PROFILE / DEFAULT_PROFILE covers
  the links (falls back to a neutral default for unknown links).
* QoS score per link combines the *static* profile (delay, loss,
  jitter) with *dynamically measured* bandwidth utilization from
  periodic OFPPortStatsRequest polling, so the "best" path can
  change at runtime as traffic load shifts - this is the "dynamic"
  part of dynamic QoS-aware routing.
* Broadcast / ARP traffic is flooded only along a spanning tree of
  the discovered topology (loop-free); unicast IP flows are routed
  along the single best QoS path and pinned with flow entries.

Run:
    ryu-manager --observe-links qos_controller.py
"""

import math
import numbers
import time
from collections import defaultdict

import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4
from ryu.lib import hub
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link


# ---------------------------------------------------------------------------
# Static QoS profile per switch-to-switch link, keyed by dpid pair.
# Keep switch DPIDs and link characteristics in sync with topology.py (s1-s4).
# ---------------------------------------------------------------------------
QOS_PROFILE = {
    # GOOD PATH: s1 -> s2 -> s4
    frozenset({1, 2}): dict(delay=5.0, bw=10.0, loss=0.5, jitter=0.5),
    frozenset({2, 4}): dict(delay=5.0, bw=10.0, loss=0.5, jitter=0.5),

    # ALTERNATIVE PATH: s1 -> s3 -> s4
    frozenset({1, 3}): dict(delay=40.0, bw=2.0, loss=5.0, jitter=5.0),
    frozenset({3, 4}): dict(delay=40.0, bw=2.0, loss=5.0, jitter=5.0),
}
DEFAULT_PROFILE = dict(delay=5.0, bw=10.0, loss=0.5, jitter=1.0)

# Scoring weights - higher score means a better link.
# Utilization is the live metric that enables congestion-aware rerouting;
# the remaining 45% preserves the original relative weighting of static QoS.
W_DELAY, W_BW, W_LOSS, W_JITTER, W_UTIL = 0.15, 0.15, 0.10, 0.05, 0.55

MONITOR_INTERVAL_SEC = 10
REROUTE_MARGIN = 0.15   # reroute only if aggregate cost is strictly >15% lower


def normalize(value, worst, best):
    """Map value into [0,1] where 1 == best. Handles best<worst too."""
    if best == worst:
        return 1.0
    frac = (value - worst) / (best - worst)
    return max(0.0, min(1.0, frac))


class QoSAwarePathSelector(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.datapaths = {}                 # dpid -> Datapath
        self.mac_to_port_edge = {}          # dpid -> {mac: port}   (host-facing ports)
        self.host_location = {}             # mac -> (dpid, port)
        self.topo_graph = nx.Graph()        # dpid nodes, QoS 'score' edges
        self.adjacency = defaultdict(dict)  # dpid -> {neighbor_dpid: out_port}
        self.switch_ports = defaultdict(set)  # dpid -> set(all physical port numbers)
        self.port_bytes = defaultdict(dict)  # (dpid, port) -> (timestamp, tx+rx bytes)
        self.active_flows = {}              # (src_mac, dst_mac) -> path (list of dpids)
        self.spanning_tree_ports = defaultdict(set)  # dpid -> set(ports) allowed to flood

        self.monitor_thread = hub.spawn(self._monitor)

    # ------------------------------------------------------------------
    # Switch connection / initial flow table setup
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        parser, ofproto = dp.ofproto_parser, dp.ofproto

        # table-miss -> send to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                           ofproto.OFPCML_NO_BUFFER)]
        self._add_flow(dp, 0, match, actions)
        self.logger.info("Switch %s connected", dp.id)

    def _add_flow(self, dp, priority, match, actions, idle_timeout=0, hard_timeout=0):
        parser, ofproto = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                 instructions=inst, idle_timeout=idle_timeout,
                                 hard_timeout=hard_timeout)
        dp.send_msg(mod)

    # ------------------------------------------------------------------
    # Topology discovery -> build graph + spanning tree for flooding
    # ------------------------------------------------------------------
    @set_ev_cls(topo_event.EventSwitchEnter)
    @set_ev_cls(topo_event.EventSwitchLeave)
    @set_ev_cls(topo_event.EventLinkAdd)
    @set_ev_cls(topo_event.EventLinkDelete)
    def _topology_change(self, ev):
        self._rebuild_topology()

    def _rebuild_topology(self):
        switches = get_switch(self, None)
        links = get_link(self, None)

        self.topo_graph.clear()
        self.adjacency.clear()
        self.switch_ports.clear()

        for sw in switches:
            self.topo_graph.add_node(sw.dp.id)
            # record every real physical port this switch has, regardless of
            # whether a host has spoken on it yet (fixes: floods must be able
            # to reach a host that hasn't transmitted anything yet)
            self.switch_ports[sw.dp.id] = {
                p.port_no for p in sw.ports if p.port_no <= 0xFFFFFF00
            }

        inter_switch_ports = defaultdict(set)
        for link in links:
            src_dpid, src_port = link.src.dpid, link.src.port_no
            dst_dpid, dst_port = link.dst.dpid, link.dst.port_no

            self.adjacency[src_dpid][dst_dpid] = src_port
            self.adjacency[dst_dpid][src_dpid] = dst_port
            inter_switch_ports[src_dpid].add(src_port)
            inter_switch_ports[dst_dpid].add(dst_port)

            try:
                score = self._link_score(src_dpid, dst_dpid)
                if not math.isfinite(score) or score < 0:
                    raise ValueError("edge score must be finite and non-negative")
                cost = self._score_to_cost(score)
            except (TypeError, ValueError, OverflowError) as exc:
                self.logger.warning("Invalid QoS metric on link %s-%s: %s",
                                    src_dpid, dst_dpid, exc)
                self.topo_graph.add_edge(src_dpid, dst_dpid)
            else:
                self.topo_graph.add_edge(src_dpid, dst_dpid, score=score, cost=cost)

        # spanning tree for loop-free flooding of ARP / broadcast
        self.spanning_tree_ports.clear()
        if self.topo_graph.number_of_nodes() > 0:
            tree = nx.minimum_spanning_tree(self.topo_graph, weight="cost")
            for u, v in tree.edges():
                self.spanning_tree_ports[u].add(self.adjacency[u][v])
                self.spanning_tree_ports[v].add(self.adjacency[v][u])

        self.logger.info("Topology rebuilt: %d switches, %d links",
                          self.topo_graph.number_of_nodes(),
                          self.topo_graph.number_of_edges())

    # ------------------------------------------------------------------
    # QoS scoring
    # ------------------------------------------------------------------
    def _link_score(self, dpid_a, dpid_b):
        """Combine static QoS profile with live bandwidth utilization."""
        profile = QOS_PROFILE.get(frozenset({dpid_a, dpid_b}), DEFAULT_PROFILE)

        n_delay = normalize(profile["delay"], worst=25.0, best=1.0)
        n_bw = normalize(profile["bw"], worst=5.0, best=100.0)
        n_loss = normalize(profile["loss"], worst=5.0, best=0.0)
        n_jitter = normalize(profile["jitter"], worst=6.0, best=0.0)

        util = self._link_utilization(dpid_a, dpid_b)  # 0 (idle) .. 1 (saturated)
        n_util = 1.0 - util

        score = (W_DELAY * n_delay + W_BW * n_bw + W_LOSS * n_loss +
                 W_JITTER * n_jitter + W_UTIL * n_util)
        return score

    def _link_utilization(self, dpid_a, dpid_b):
        """Estimate current utilization (0..1) of the link a<->b from
        the most recent port-stats sample on a's outgoing port."""
        port = self.adjacency.get(dpid_a, {}).get(dpid_b)
        if port is None:
            return 0.0
        sample = self.port_bytes.get((dpid_a, port))
        if not sample or "rate_mbps" not in sample:
            return 0.0
        profile = QOS_PROFILE.get(frozenset({dpid_a, dpid_b}), DEFAULT_PROFILE)
        capacity = max(profile["bw"], 1.0)
        return max(0.0, min(1.0, sample["rate_mbps"] / capacity))

    @staticmethod
    def _score_to_cost(score):
        # networkx path algorithms minimize cost; invert the score.
        if isinstance(score, bool) or not isinstance(score, numbers.Real):
            raise ValueError("edge score must be a finite non-negative number")
        try:
            score = float(score)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("edge score must be a finite non-negative number") from exc
        if not math.isfinite(score) or score < 0:
            raise ValueError("edge score must be a finite non-negative number")
        return 1.0 / max(score, 1e-3)

    def _update_link_cost(self):
        for u, v in self.topo_graph.edges():
            try:
                score = self._link_score(u, v)
                if not math.isfinite(score) or score < 0:
                    raise ValueError("edge score must be finite and non-negative")
                cost = self._score_to_cost(score)
            except (TypeError, ValueError, OverflowError) as exc:
                self.topo_graph[u][v].pop("score", None)
                self.topo_graph[u][v].pop("cost", None)
                self.logger.warning("Invalid QoS metric on link %s-%s: %s", u, v, exc)
                continue
            self.topo_graph[u][v]["score"] = score
            self.topo_graph[u][v]["cost"] = cost

    # ------------------------------------------------------------------
    # Periodic monitoring: poll port stats, refresh QoS scores, re-route
    # ------------------------------------------------------------------
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_port_stats(dp)
            hub.sleep(MONITOR_INTERVAL_SEC)
            self._update_link_cost()
            try:
                self._reoptimize_active_flows()
            except Exception:
                # A monitor-cycle failure should be visible without killing
                # subsequent statistics and rerouting cycles.
                self.logger.exception("Unexpected error while reoptimizing active flows")

    def _request_port_stats(self, dp):
        parser = dp.ofproto_parser
        req = parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
        dp.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()
        for stat in ev.msg.body:
            key = (dpid, stat.port_no)
            total_bytes = stat.tx_bytes + stat.rx_bytes
            prev = self.port_bytes.get(key)
            if prev and "ts" in prev:
                dt = max(now - prev["ts"], 0.1)
                rate_mbps = ((total_bytes - prev["bytes"]) * 8 / 1e6) / dt
            else:
                rate_mbps = 0.0
            self.port_bytes[key] = {"ts": now, "bytes": total_bytes,
                                     "rate_mbps": rate_mbps}

    # ------------------------------------------------------------------
    # Path selection
    # ------------------------------------------------------------------
    def _best_path(self, src_dpid, dst_dpid):
        if src_dpid not in self.topo_graph or dst_dpid not in self.topo_graph:
            self.logger.warning("Cannot calculate path for missing switch %s or %s",
                                src_dpid, dst_dpid)
            return None
        if src_dpid == dst_dpid:
            return [src_dpid]

        # Keep malformed links in the topology graph for other features, but
        # exclude them from path selection. NetworkX treats missing weights as
        # 1.0, so passing the original graph would make those links usable.
        path_graph = nx.Graph()
        path_graph.add_nodes_from(self.topo_graph.nodes)
        for u, v, attrs in self.topo_graph.edges(data=True):
            if not self._valid_path_cost(attrs.get("cost")):
                continue
            path_graph.add_edge(u, v, cost=float(attrs["cost"]))
        try:
            path = nx.shortest_path(path_graph, src_dpid, dst_dpid, weight="cost")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        if self._path_cost(path) is None:
            self.logger.warning("Cannot use selected path %s->%s: aggregate cost is invalid",
                                src_dpid, dst_dpid)
            return None
        return path

    @staticmethod
    def _valid_path_cost(cost):
        if isinstance(cost, bool) or not isinstance(cost, numbers.Real):
            return False
        try:
            numeric_cost = float(cost)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(numeric_cost) and numeric_cost >= 0

    def _path_cost(self, path):
        """Return a valid path's summed edge cost, or None if it is stale/bad."""
        if not path:
            return None
        if any(switch not in self.topo_graph for switch in path):
            return None

        total = 0.0
        for u, v in zip(path, path[1:]):
            if not self.topo_graph.has_edge(u, v):
                return None
            cost = self.topo_graph[u][v].get("cost")
            if not self._valid_path_cost(cost):
                return None
            total += float(cost)
            if not math.isfinite(total):
                return None
        return total

    def _reoptimize_active_flows(self):
        """Continuous feedback loop: if a materially better path now
        exists for a live flow, re-pin it (matches 'Monitor Again' in
        the methodology)."""
        processed_pairs = set()
        for (src_mac, dst_mac), old_path in list(self.active_flows.items()):
            pair = frozenset((src_mac, dst_mac))
            if pair in processed_pairs:
                continue
            processed_pairs.add(pair)

            try:
                if src_mac not in self.host_location or dst_mac not in self.host_location:
                    self.logger.warning("Skipping flow %s->%s: endpoint location is unknown",
                                        src_mac, dst_mac)
                    continue
                src_dpid = self.host_location[src_mac][0]
                dst_dpid = self.host_location[dst_mac][0]
                new_path = self._best_path(src_dpid, dst_dpid)
                if not new_path:
                    continue

                new_cost = self._path_cost(new_path)
                old_cost = self._path_cost(old_path)
                if new_cost is None:
                    self.logger.warning("Skipping flow %s->%s: selected path has invalid cost",
                                        src_mac, dst_mac)
                    continue
                if new_path == old_path and old_cost is not None:
                    continue

                # A stale path must be replaced when a valid route exists.
                # A zero-cost old route cannot be improved by any valid
                # non-negative route.
                should_reroute = old_cost is None or (
                    old_cost > 0 and new_cost < old_cost * (1 - REROUTE_MARGIN)
                )
                if not should_reroute:
                    continue

                reverse_path = list(reversed(new_path))
                self.logger.info("Re-routing %s->%s : %s -> %s (cost %.6g -> %.6g)",
                                 src_mac, dst_mac, old_path, new_path,
                                 old_cost if old_cost is not None else float("nan"), new_cost)
                forward_ok = self.install_path(new_path, src_mac, dst_mac)
                reverse_ok = self.install_path(reverse_path, dst_mac, src_mac)
                if forward_ok and reverse_ok:
                    self.active_flows[(src_mac, dst_mac)] = list(new_path)
                    self.active_flows[(dst_mac, src_mac)] = reverse_path
                else:
                    self.logger.warning(
                        "Flow reroute incomplete for %s<->%s; retaining recorded paths",
                        src_mac, dst_mac)
            except Exception:
                self.logger.exception("Failed to reoptimize flow %s->%s", src_mac, dst_mac)

    # ------------------------------------------------------------------
    # Packet-in: learn hosts, resolve ARP/flood, install QoS paths
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        in_port = msg.match["in_port"]
        parser, ofproto = dp.ofproto_parser, dp.ofproto

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        src_mac, dst_mac = eth.src, eth.dst

        # learn where this host is attached
        # Learn hosts only on host-facing access ports
        if in_port in self._access_ports(dpid):
            if self.host_location.get(src_mac) != (dpid, in_port):
                self.host_location[src_mac] = (dpid, in_port)
                self.logger.info("Learned host %s at switch %s port %s", src_mac, dpid, in_port)

        # unicast, both endpoints known -> compute & install best QoS path
        if dst_mac in self.host_location and dst_mac != "ff:ff:ff:ff:ff:ff":
            src_dpid = self.host_location[src_mac][0]
            dst_dpid = self.host_location[dst_mac][0]
            path = self._best_path(src_dpid, dst_dpid)
            if path:
                if self.install_path(path, src_mac, dst_mac):
                    self.active_flows[(src_mac, dst_mac)] = list(path)
                    self.active_flows[(dst_mac, src_mac)] = list(reversed(path))
                    # Also forward this first packet along the selected path.
                    self._forward_packet_out(dp, msg, path, dst_mac, in_port)
                    return

        # unknown destination / broadcast (ARP etc.) -> flood on spanning tree
        self._flood(dp, msg, in_port)

    def _flood(self, dp, msg, in_port):
        parser, ofproto = dp.ofproto_parser, dp.ofproto
        dpid = dp.id
        out_ports = self.spanning_tree_ports.get(dpid, set()) | self._access_ports(dpid)
        out_ports = [p for p in out_ports if p != in_port]

        actions = [parser.OFPActionOutput(p) for p in out_ports] or \
            [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)

    def _access_ports(self, dpid):
        """Ports on this switch that are NOT inter-switch links - i.e. every
        port a host could plausibly be attached to, known or not yet heard
        from. Using only *already-learned* hosts here would mean a host that
        has never transmitted (e.g. waiting for the very first ARP to reach
        it) could never receive that flood - a chicken-and-egg deadlock."""
        inter = set(self.adjacency.get(dpid, {}).values())
        all_ports = self.switch_ports.get(dpid, set())
        return all_ports - inter

    def _forward_packet_out(self, dp, msg, path, dst_mac, in_port):
        out_port = self._first_hop_out_port(path, dst_mac)
        if out_port is None:
            return
        parser, ofproto = dp.ofproto_parser, dp.ofproto
        actions = [parser.OFPActionOutput(out_port)]
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                   in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)

    def _first_hop_out_port(self, path, dst_mac):
        if len(path) >= 2:
            return self.adjacency.get(path[0], {}).get(path[1])
        return self.host_location.get(dst_mac, (None, None))[1]

    # ------------------------------------------------------------------
    # Install flow entries for every hop along the chosen QoS path
    # ------------------------------------------------------------------
    def install_path(self, path, src_mac, dst_mac):
        if self._path_cost(path) is None:
            self.logger.warning("Cannot dispatch flow %s->%s: path is invalid: %s",
                                src_mac, dst_mac, path)
            return False

        required = []
        for i, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                self.logger.warning("Cannot dispatch flow %s->%s: switch %s is unavailable",
                                    src_mac, dst_mac, dpid)
                return False

            if i < len(path) - 1:
                out_port = self.adjacency.get(dpid, {}).get(path[i + 1])
            else:
                out_port = self.host_location.get(dst_mac, (None, None))[1]
            if not isinstance(out_port, int) or out_port <= 0:
                self.logger.warning(
                    "Cannot dispatch flow %s->%s: no valid output port on switch %s",
                    src_mac, dst_mac, dpid)
                return False
            required.append((dp, out_port))

        dispatched = True
        for dp, out_port in required:
            try:
                parser = dp.ofproto_parser
                match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
                actions = [parser.OFPActionOutput(out_port)]
                self._add_flow(dp, priority=10, match=match, actions=actions,
                               idle_timeout=60, hard_timeout=0)
            except Exception:
                dispatched = False
                self.logger.exception("Failed to dispatch flow rule for %s->%s on switch %s",
                                      src_mac, dst_mac, getattr(dp, "id", "unknown"))

        if dispatched:
            self.logger.info("Dispatched flow rules for %s->%s along %s (cost=%.6g)",
                             src_mac, dst_mac, path, self._path_cost(path))
        return dispatched
