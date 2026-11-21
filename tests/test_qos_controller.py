import math
import logging
from collections import defaultdict
from types import SimpleNamespace

import networkx as nx
import pytest

from qos_controller import (
    QoSAwarePathSelector, W_DELAY, W_BW, W_LOSS, W_JITTER, W_UTIL,
)


def make_controller(edges=(), nodes=()):
    controller = object.__new__(QoSAwarePathSelector)
    controller.topo_graph = nx.Graph()
    controller.topo_graph.add_nodes_from(nodes)
    for left, right, cost in edges:
        controller.topo_graph.add_edge(left, right, cost=cost)
    controller.active_flows = {}
    controller.host_location = {}
    controller.adjacency = defaultdict(dict)
    controller.switch_ports = defaultdict(set)
    controller.datapaths = {}
    controller.logger = logging.getLogger("qos-controller-test")
    return controller


def add_flow_pair(controller, src="aa", dst="bb", old_path=(1, 2)):
    controller.host_location[src] = (old_path[0], 10)
    controller.host_location[dst] = (old_path[-1], 20)
    controller.active_flows[(src, dst)] = list(old_path)
    controller.active_flows[(dst, src)] = list(reversed(old_path))


def run_packet_in(controller, monkeypatch, src_mac, dst_mac):
    import qos_controller as controller_module

    class FakePacket:
        def __init__(self, data):
            pass

        def get_protocol(self, protocol):
            return SimpleNamespace(src=src_mac, dst=dst_mac, ethertype=0x0800)

    monkeypatch.setattr(controller_module.packet, "Packet", FakePacket, raising=False)
    monkeypatch.setattr(controller_module.ethernet, "ethernet", object, raising=False)
    monkeypatch.setattr(controller_module.ether_types, "ETH_TYPE_LLDP", 0x88CC, raising=False)
    datapath = SimpleNamespace(id=1, ofproto_parser=None, ofproto=None)
    message = SimpleNamespace(datapath=datapath, match={"in_port": 11}, data=b"", buffer_id=0)
    controller.packet_in_handler(SimpleNamespace(msg=message))


def route_graph(new_costs=(4.25, 4.25)):
    return make_controller(
        edges=[
            (1, 2, 5.0), (2, 4, 5.0),
            (1, 3, new_costs[0]), (3, 4, new_costs[1]),
        ],
        nodes=(1, 2, 3, 4),
    )


def test_best_path_minimizes_sum_of_edge_costs():
    controller = make_controller(
        edges=[(1, 2, 1.0), (2, 4, 2.0), (1, 3, 1.6), (3, 4, 1.6)],
        nodes=(1, 2, 3, 4),
    )

    assert controller._best_path(1, 4) == [1, 2, 4]


def test_qos_weights_enable_congestion_reroute_at_high_utilization():
    controller = make_controller()
    assert sum((W_DELAY, W_BW, W_LOSS, W_JITTER, W_UTIL)) == pytest.approx(1.0)
    assert W_UTIL > W_DELAY + W_BW + W_LOSS + W_JITTER

    # At 80% utilization on the preferred route, the idle alternate route
    # clears the strict 15% margin and is selected by the normal reroute path.
    pairs = ((1, 2), (2, 4), (1, 3), (3, 4))
    def edge_cost(left, right, utilization):
        controller._link_utilization = lambda a, b: utilization if frozenset((a, b)) == frozenset((left, right)) else 0.0
        return 1.0 / controller._link_score(left, right)

    good_costs = [edge_cost(a, b, 0.0) for a, b in pairs[:2]]
    alternate_costs = [edge_cost(a, b, 0.0) for a, b in pairs[2:]]
    edges = [(1, 2, good_costs[0]), (2, 4, good_costs[1]),
             (1, 3, alternate_costs[0]), (3, 4, alternate_costs[1])]
    controller = make_controller(edges=edges, nodes=(1, 2, 3, 4))
    controller._link_utilization = lambda left, right: (
        0.80 if frozenset((left, right)) in (frozenset((1, 2)), frozenset((2, 4))) else 0.0
    )
    good_cost = sum(1.0 / controller._link_score(a, b) for a, b in pairs[:2])
    alternate_cost = sum(1.0 / controller._link_score(a, b) for a, b in pairs[2:])
    assert alternate_cost < good_cost * 0.85

    add_flow_pair(controller, old_path=(1, 2, 4))
    dispatched = []
    controller.install_path = lambda path, src, dst: dispatched.append(list(path)) or True
    controller._update_link_cost()
    controller._reoptimize_active_flows()
    assert controller.active_flows[("aa", "bb")] == [1, 3, 4]
    assert controller.active_flows[("bb", "aa")] == [4, 3, 1]
    assert dispatched == [[1, 3, 4], [4, 3, 1]]

def test_path_cost_sums_edges_and_single_switch_cost_is_zero():
    controller = make_controller(edges=[(1, 2, 2.5), (2, 3, 1.25)], nodes=(1, 2, 3))

    assert controller._path_cost([1, 2, 3]) == pytest.approx(3.75)
    assert controller._path_cost([2]) == 0


def test_path_cost_rejects_an_overflowed_aggregate():
    controller = make_controller(edges=[(1, 2, 1e308), (2, 3, 1e308)], nodes=(1, 2, 3))

    assert controller._path_cost([1, 2, 3]) is None


@pytest.mark.parametrize("bad_cost", [None, -1.0, float("nan"), float("inf"), float("-inf"), "1.0"])
def test_path_cost_rejects_missing_invalid_negative_and_nonfinite_costs(bad_cost):
    controller = make_controller(nodes=(1, 2))
    if bad_cost is not None:
        controller.topo_graph.add_edge(1, 2, cost=bad_cost)
    else:
        controller.topo_graph.add_edge(1, 2)

    assert controller._path_cost([1, 2]) is None


def test_best_path_ignores_unrelated_invalid_costs():
    controller = make_controller(
        edges=[(1, 2, 1.0), (3, 4, float("nan"))], nodes=(1, 2, 3, 4)
    )

    assert controller._best_path(1, 2) == [1, 2]
    assert math.isnan(controller.topo_graph[3][4]["cost"])


def test_best_path_does_not_select_missing_cost_edge_over_valid_alternative():
    controller = make_controller(
        edges=[(1, 2, None), (1, 3, 2.0), (3, 2, 2.0)], nodes=(1, 2, 3)
    )
    controller.topo_graph[1][2].pop("cost")

    assert controller._best_path(1, 2) == [1, 3, 2]


def test_best_path_returns_none_when_all_routes_have_invalid_costs():
    controller = make_controller(
        edges=[(1, 2, None), (2, 4, 1.0), (1, 3, float("nan")), (3, 4, 1.0)],
        nodes=(1, 2, 3, 4),
    )
    controller.topo_graph[1][2].pop("cost")

    assert controller._best_path(1, 4) is None


def test_invalid_link_score_clears_stale_path_cost():
    controller = make_controller(edges=[(1, 2, 1.0)], nodes=(1, 2))
    controller.topo_graph[1][2]["score"] = 0.5
    controller._link_score = lambda *args: float("nan")

    controller._update_link_cost()

    assert "score" not in controller.topo_graph[1][2]
    assert "cost" not in controller.topo_graph[1][2]


def test_best_path_handles_disconnected_and_missing_endpoints():
    controller = make_controller(edges=[(1, 2, 1.0)], nodes=(1, 2, 3))

    assert controller._best_path(1, 3) is None
    assert controller._best_path(1, 99) is None
    assert controller._best_path(99, 99) is None


def test_removed_edge_invalidates_old_path_and_reroutes():
    controller = make_controller(edges=[(1, 3, 1.0), (3, 4, 1.0)], nodes=(1, 2, 3, 4))
    add_flow_pair(controller, old_path=(1, 2, 4))
    dispatched = []
    controller.install_path = lambda path, src, dst: dispatched.append((list(path), src, dst)) or True

    controller._reoptimize_active_flows()

    assert controller.active_flows[("aa", "bb")] == [1, 3, 4]
    assert controller.active_flows[("bb", "aa")] == [4, 3, 1]
    assert len(dispatched) == 2


def test_exactly_fifteen_percent_improvement_does_not_reroute():
    controller = route_graph((4.25, 4.25))
    add_flow_pair(controller, old_path=(1, 2, 4))
    dispatched = []
    controller.install_path = lambda *args: dispatched.append(args) or True

    controller._reoptimize_active_flows()

    assert dispatched == []
    assert controller.active_flows[("aa", "bb")] == [1, 2, 4]


@pytest.mark.parametrize("new_costs,should_reroute", [
    ((4.24, 4.24), True),
    ((4.30, 4.30), False),
])
def test_reroute_uses_strict_fifteen_percent_threshold(new_costs, should_reroute):
    controller = route_graph(new_costs)
    add_flow_pair(controller, old_path=(1, 2, 4))
    dispatched = []
    controller.install_path = lambda path, src, dst: dispatched.append((list(path), src, dst)) or True

    controller._reoptimize_active_flows()

    assert bool(dispatched) is should_reroute
    if should_reroute:
        assert {tuple(item[0]) for item in dispatched} == {(1, 3, 4), (4, 3, 1)}


def test_successful_reroute_updates_both_directions_and_is_not_repeated():
    controller = route_graph((4.0, 4.0))
    add_flow_pair(controller, old_path=(1, 2, 4))
    dispatched = []
    controller.install_path = lambda path, src, dst: dispatched.append((list(path), src, dst)) or True

    controller._reoptimize_active_flows()
    controller._reoptimize_active_flows()

    assert controller.active_flows[("aa", "bb")] == [1, 3, 4]
    assert controller.active_flows[("bb", "aa")] == [4, 3, 1]
    assert len(dispatched) == 2


def test_incomplete_forward_reverse_dispatch_does_not_update_state():
    controller = route_graph((4.0, 4.0))
    add_flow_pair(controller, old_path=(1, 2, 4))
    outcomes = iter((True, False))
    controller.install_path = lambda *args: next(outcomes)

    controller._reoptimize_active_flows()

    assert controller.active_flows[("aa", "bb")] == [1, 2, 4]
    assert controller.active_flows[("bb", "aa")] == [4, 2, 1]


def test_forward_reroute_failure_skips_reverse_installation_and_keeps_paths():
    controller = route_graph((4.0, 4.0))
    add_flow_pair(controller, old_path=(1, 2, 4))
    attempts = []

    def fail_forward(path, src, dst):
        attempts.append((list(path), src, dst))
        return False

    controller.install_path = fail_forward

    controller._reoptimize_active_flows()

    assert attempts == [([1, 3, 4], "aa", "bb")]
    assert controller.active_flows[("aa", "bb")] == [1, 2, 4]
    assert controller.active_flows[("bb", "aa")] == [4, 2, 1]


def test_disconnected_flow_does_not_stop_other_flows():
    controller = make_controller(
        edges=[(1, 2, 5.0), (2, 4, 5.0), (1, 3, 4.0), (3, 4, 4.0)],
        nodes=(1, 2, 3, 4, 5, 6),
    )
    add_flow_pair(controller, src="a", dst="b", old_path=(1, 2, 4))
    add_flow_pair(controller, src="c", dst="d", old_path=(5, 6))
    dispatched = []
    controller.install_path = lambda path, src, dst: dispatched.append((list(path), src, dst)) or True

    controller._reoptimize_active_flows()

    assert controller.active_flows[("a", "b")] == [1, 3, 4]
    assert controller.active_flows[("d", "c")] == [6, 5]
    assert len(dispatched) == 2


class FakeParser:
    def OFPMatch(self, **kwargs):
        return kwargs

    def OFPActionOutput(self, port):
        return ("output", port)

    def OFPInstructionActions(self, instruction_type, actions):
        return ("instructions", instruction_type, actions)

    def OFPFlowMod(self, **kwargs):
        return kwargs


class FakeDatapath:
    def __init__(self, fail=False):
        self.ofproto_parser = FakeParser()
        self.ofproto = SimpleNamespace(OFPIT_APPLY_ACTIONS=4)
        self.messages = []
        self.fail = fail

    def send_msg(self, message):
        if self.fail:
            raise RuntimeError("dispatch failed")
        self.messages.append(message)


def installable_controller(path=(1, 2)):
    controller = make_controller(edges=[(1, 2, 1.0)], nodes=(1, 2))
    controller.adjacency[1][2] = 12
    controller.host_location["bb"] = (path[-1], 22)
    controller.datapaths = {1: FakeDatapath(), 2: FakeDatapath()}
    return controller


def test_install_path_reports_success_when_all_flow_mods_are_dispatched():
    controller = installable_controller()

    assert controller.install_path([1, 2], "aa", "bb") is True
    assert len(controller.datapaths[1].messages) == 1
    assert len(controller.datapaths[2].messages) == 1


@pytest.mark.parametrize("missing", ["datapath", "port"])
def test_install_path_reports_missing_datapath_or_output_port(missing):
    controller = installable_controller()
    if missing == "datapath":
        del controller.datapaths[2]
    else:
        controller.adjacency[1].clear()

    assert controller.install_path([1, 2], "aa", "bb") is False
    assert all(not dp.messages for dp in controller.datapaths.values())


def test_install_path_reports_send_failure():
    controller = installable_controller()
    controller.datapaths[2].fail = True

    assert controller.install_path([1, 2], "aa", "bb") is False


def test_install_path_logs_partial_dispatch_without_claiming_switch_installation(caplog):
    controller = installable_controller()
    controller.datapaths[2].fail = True

    assert controller.install_path([1, 2], "aa", "bb") is False

    assert len(controller.datapaths[1].messages) == 1
    assert "dispatched 1 of 2" in caplog.text
    assert "switch-side installation is unconfirmed" in caplog.text


def test_monitor_continues_after_one_datapath_stats_request_fails(monkeypatch, caplog):
    import qos_controller as controller_module

    class StatsParser:
        def OFPPortStatsRequest(self, datapath, flags, port):
            return ("stats-request", datapath.id, flags, port)

    class StatsDatapath:
        def __init__(self, dpid, fail_once=False):
            self.id = dpid
            self.ofproto_parser = StatsParser()
            self.ofproto = SimpleNamespace(OFPP_ANY=0xFFFFFFFF)
            self.fail_once = fail_once
            self.attempts = 0
            self.messages = []

        def send_msg(self, message):
            self.attempts += 1
            if self.fail_once and self.attempts == 1:
                raise RuntimeError("datapath disconnected")
            self.messages.append(message)

    controller = make_controller()
    failing_dp = StatsDatapath(1, fail_once=True)
    healthy_dp = StatsDatapath(2)
    controller.datapaths = {1: failing_dp, 2: healthy_dp}
    sleep_calls = 0

    def stop_after_second_cycle(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            raise RuntimeError("stop monitor test")

    monkeypatch.setattr(controller_module.hub, "sleep", stop_after_second_cycle)

    with pytest.raises(RuntimeError, match="stop monitor test"):
        controller._monitor()

    assert failing_dp.attempts == 2
    assert healthy_dp.attempts == 2
    assert len(healthy_dp.messages) == 2
    assert "Failed to request port statistics from switch 1" in caplog.text


def test_monitor_continues_cycle_after_link_cost_refresh_raises(monkeypatch, caplog):
    import qos_controller as controller_module

    controller = make_controller()
    sleep_calls = 0
    refresh_calls = 0
    reoptimize_calls = 0

    def stop_after_second_cycle(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 3:
            raise RuntimeError("stop monitor test")

    def refresh_link_costs():
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            raise RuntimeError("unexpected metric processing error")

    def reoptimize_flows():
        nonlocal reoptimize_calls
        reoptimize_calls += 1

    controller._update_link_cost = refresh_link_costs
    controller._reoptimize_active_flows = reoptimize_flows
    monkeypatch.setattr(controller_module.hub, "sleep", stop_after_second_cycle)

    with pytest.raises(RuntimeError, match="stop monitor test"):
        controller._monitor()

    assert refresh_calls == 2
    assert reoptimize_calls == 2
    assert "Unexpected error while updating link costs" in caplog.text


def test_same_switch_path_installs_host_output():
    controller = make_controller(nodes=(1,))
    controller.host_location["bb"] = (1, 22)
    controller.datapaths[1] = FakeDatapath()

    assert controller.install_path([1], "aa", "bb") is True
    assert len(controller.datapaths[1].messages) == 1


def test_same_switch_flow_has_zero_cost_and_is_not_reoptimized():
    controller = make_controller(edges=[(2, 3, float("nan"))], nodes=(1, 2, 3))
    add_flow_pair(controller, old_path=(1,))
    dispatched = []
    controller.install_path = lambda *args: dispatched.append(args) or True

    assert controller._best_path(1, 1) == [1]
    assert controller._path_cost([1]) == 0
    controller._reoptimize_active_flows()

    assert dispatched == []


def test_score_to_cost_rejects_invalid_scores():
    for score in (-1, float("nan"), float("inf"), "1.0"):
        with pytest.raises(ValueError):
            QoSAwarePathSelector._score_to_cost(score)


def test_packet_in_only_records_flow_state_after_successful_installation(monkeypatch):
    import qos_controller as controller_module

    controller = make_controller(edges=[(1, 2, 1.0)], nodes=(1, 2))
    controller._best_path = lambda src, dst: [1, 2]
    controller.install_path = lambda *args: False
    controller._forward_packet_out = lambda *args: pytest.fail("must not forward after failed installation")
    controller._flood = lambda *args: None
    controller.host_location = {"aa": (1, 11), "bb": (2, 22)}
    class FakePacket:
        def __init__(self, data):
            pass

        def get_protocol(self, protocol):
            return SimpleNamespace(src="aa", dst="bb", ethertype=0x0800)

    monkeypatch.setattr(controller_module.packet, "Packet", FakePacket, raising=False)
    monkeypatch.setattr(controller_module.ethernet, "ethernet", object, raising=False)
    monkeypatch.setattr(controller_module.ether_types, "ETH_TYPE_LLDP", 0x88CC, raising=False)
    datapath = SimpleNamespace(id=1, ofproto_parser=None, ofproto=None)
    message = SimpleNamespace(datapath=datapath, match={"in_port": 11}, data=b"", buffer_id=0)
    controller.packet_in_handler(SimpleNamespace(msg=message))

    assert controller.active_flows == {}


def test_known_destination_without_route_does_not_flood(monkeypatch, caplog):
    controller = make_controller(nodes=(1, 2))
    controller.host_location = {"aa": (1, 11), "bb": (2, 22)}
    controller._flood = lambda *args: pytest.fail("known destination must not be flooded")

    run_packet_in(controller, monkeypatch, "aa", "bb")

    assert "No route available for known destination bb" in caplog.text


def test_known_destination_installation_failure_does_not_flood(monkeypatch, caplog):
    controller = make_controller(edges=[(1, 2, 1.0)], nodes=(1, 2))
    controller.host_location = {"aa": (1, 11), "bb": (2, 22)}
    controller.install_path = lambda *args: False
    controller._flood = lambda *args: pytest.fail("failed known unicast must not be flooded")

    run_packet_in(controller, monkeypatch, "aa", "bb")

    assert "Failed to install path for known destination aa->bb" in caplog.text


@pytest.mark.parametrize("dst_mac", ["cc", "ff:ff:ff:ff:ff:ff"])
def test_unknown_destination_and_broadcast_keep_flooding(monkeypatch, dst_mac):
    controller = make_controller()
    controller.host_location = {"aa": (1, 11)}
    flooded = []
    controller._flood = lambda *args: flooded.append(args)

    run_packet_in(controller, monkeypatch, "aa", dst_mac)

    assert len(flooded) == 1


def test_invalid_flow_does_not_prevent_other_flow_processing():
    controller = route_graph((4.0, 4.0))
    add_flow_pair(controller, src="a", dst="b", old_path=(5, 6))
    controller.active_flows[("c", "d")] = [1, 2, 4]
    controller.active_flows[("d", "c")] = [4, 2, 1]
    controller.host_location.update({"a": (5, 10), "b": (6, 20), "c": (1, 10), "d": (4, 20)})
    dispatched = []
    controller.install_path = lambda path, src, dst: dispatched.append((list(path), src, dst)) or True

    controller._reoptimize_active_flows()

    assert len(dispatched) == 2
    assert controller.active_flows[("c", "d")] == [1, 3, 4]
