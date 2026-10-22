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
* Two edge switches (s1, s2 in topology.py) are host-facing; three
  core switches (s3, s4, s5) each provide one alternate path between
  them, with distinct static QoS characteristics (delay / bandwidth /
  loss) mirroring the physical link settings in topology.py.
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
# Keep in sync with LINK_PROFILES in topology.py (s1=1, s2=2, s3=3, s4=4, s5=5).
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

# Scoring weights - tune to taste. Higher score == better path.
W_DELAY, W_BW, W_LOSS, W_JITTER, W_UTIL = 0.30, 0.30, 0.20, 0.10, 0.10

MONITOR_INTERVAL_SEC = 10
REROUTE_MARGIN = 0.15   # only reroute an active flow if new path is >=15% better


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

            score = self._link_score(src_dpid, dst_dpid)
            self.topo_graph.add_edge(src_dpid, dst_dpid, score=score,
                                      cost=self._score_to_cost(score))

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
        return 1.0 / max(score, 1e-3)

    def _update_link_cost(self):
        for u, v in self.topo_graph.edges():
            score = self._link_score(u, v)
            self.topo_graph[u][v]["score"] = score
            self.topo_graph[u][v]["cost"] = self._score_to_cost(score)

    # ------------------------------------------------------------------
    # Periodic monitoring: poll port stats, refresh QoS scores, re-route
    # ------------------------------------------------------------------
    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                self._request_port_stats(dp)
            hub.sleep(MONITOR_INTERVAL_SEC)
            self._update_link_cost()
            self._reoptimize_active_flows()

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
        if src_dpid == dst_dpid:
            return [src_dpid]
        try:
            return nx.shortest_path(self.topo_graph, src_dpid, dst_dpid, weight="cost")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def _path_score(self, path):
        if len(path) < 2:
            return float("inf")
        return min(self.topo_graph[u][v]["score"] for u, v in zip(path, path[1:]))

    def _reoptimize_active_flows(self):
        """Continuous feedback loop: if a materially better path now
        exists for a live flow, re-pin it (matches 'Monitor Again' in
        the methodology)."""
        for (src_mac, dst_mac), old_path in list(self.active_flows.items()):
            if src_mac not in self.host_location or dst_mac not in self.host_location:
                continue
            src_dpid = self.host_location[src_mac][0]
            dst_dpid = self.host_location[dst_mac][0]
            new_path = self._best_path(src_dpid, dst_dpid)
            if not new_path or new_path == old_path:
                continue
            if self._path_score(new_path) > self._path_score(old_path) * (1 + REROUTE_MARGIN):
                self.logger.info("Re-routing %s->%s : %s -> %s",
                                  src_mac, dst_mac, old_path, new_path)
                self.install_path(new_path, src_mac, dst_mac)

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
        if self.host_location.get(src_mac) != (dpid, in_port):
            self.host_location[src_mac] = (dpid, in_port)
            self.logger.info("Learned host %s at switch %s port %s", src_mac, dpid, in_port)

        # unicast, both endpoints known -> compute & install best QoS path
        if dst_mac in self.host_location and dst_mac != "ff:ff:ff:ff:ff:ff":
            src_dpid = self.host_location[src_mac][0]
            dst_dpid = self.host_location[dst_mac][0]
            path = self._best_path(src_dpid, dst_dpid)
            if path:
                self.install_path(path, src_mac, dst_mac)
                self.active_flows[(src_mac, dst_mac)] = path
                self.active_flows[(dst_mac, src_mac)] = list(reversed(path))
                # also forward this first packet along the path immediately
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
        for i, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue
            parser = dp.ofproto_parser

            if i < len(path) - 1:
                out_port = self.adjacency.get(dpid, {}).get(path[i + 1])
            else:
                out_port = self.host_location.get(dst_mac, (None, None))[1]
            if out_port is None:
                continue

            match = parser.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            actions = [parser.OFPActionOutput(out_port)]
            self._add_flow(dp, priority=10, match=match, actions=actions,
                            idle_timeout=60, hard_timeout=0)

        self.logger.info("Installed QoS path %s->%s : %s (score=%.3f)",
                          src_mac, dst_mac, path, self._path_score(path))
