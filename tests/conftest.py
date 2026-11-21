"""Minimal Ryu import stubs for controller unit tests."""

import sys
from types import ModuleType


def _module(name):
    module = ModuleType(name)
    sys.modules[name] = module
    return module


if "ryu" not in sys.modules:
    ryu = _module("ryu")
    ryu.__path__ = []

    base = _module("ryu.base")
    base.__path__ = []
    app_manager = _module("ryu.base.app_manager")

    class RyuApp:
        def __init__(self, *args, **kwargs):
            pass

    app_manager.RyuApp = RyuApp

    controller = _module("ryu.controller")
    controller.__path__ = []
    ofp_event = _module("ryu.controller.ofp_event")
    for name in ("EventOFPSwitchFeatures", "EventOFPPortStatsReply", "EventOFPPacketIn"):
        setattr(ofp_event, name, type(name, (), {}))
    handler = _module("ryu.controller.handler")
    handler.CONFIG_DISPATCHER = object()
    handler.MAIN_DISPATCHER = object()
    handler.set_ev_cls = lambda *args, **kwargs: lambda fn: fn

    ofproto = _module("ryu.ofproto")
    ofproto.__path__ = []
    ofproto_v1_3 = _module("ryu.ofproto.ofproto_v1_3")
    ofproto_v1_3.OFP_VERSION = 4

    lib = _module("ryu.lib")
    lib.__path__ = []
    packet = _module("ryu.lib.packet")
    for name in ("packet", "ethernet", "ether_types", "arp", "ipv4"):
        setattr(packet, name, ModuleType(f"ryu.lib.packet.{name}"))
    hub = _module("ryu.lib.hub")
    hub.spawn = lambda fn: None
    hub.sleep = lambda seconds: None

    topology = _module("ryu.topology")
    topology.__path__ = []
    event = _module("ryu.topology.event")
    for name in ("EventSwitchEnter", "EventSwitchLeave", "EventLinkAdd", "EventLinkDelete"):
        setattr(event, name, type(name, (), {}))
    api = _module("ryu.topology.api")
    api.get_switch = lambda *args: []
    api.get_link = lambda *args: []
