#!/usr/bin/env python3
r"""
topology.py
Custom Mininet topology for "Dynamic QoS-Aware Path Selection Using SDN".

Layout:

        h1                                   h2
         \                                   /
          s1 ----- s2 (GOOD path: high bw, low delay, low loss) ----- s4
           \                                                         /
            ---- s3 (BAD path: low bw, high delay, high loss) ------

Two independent paths exist between h1 and h2 (s1-s2-s4 and s1-s3-s4),
deliberately given different QoS characteristics with Mininet's TCLink so
the Ryu controller has something real to choose between.

Run with:
    sudo mn --custom topology.py --topo qostopo --controller remote --switch ovs,protocols=OpenFlow13
or:
    sudo python3 topology.py
"""

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel, info


class QoSTopo(Topo):
    """Two switches-wide diamond topology with asymmetric link QoS."""

    def build(self):
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')

        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', protocols='OpenFlow13')
        s4 = self.addSwitch('s4', protocols='OpenFlow13')

        # Edge links (host <-> switch): treat as ideal
        self.addLink(h1, s1, cls=TCLink, bw=100, delay='1ms')
        self.addLink(h2, s4, cls=TCLink, bw=100, delay='1ms')

        # GOOD path: s1 - s2 - s4  (high bandwidth, low delay, ~0 loss)
        self.addLink(s1, s2, cls=TCLink, bw=10, delay='5ms', loss=0.5)
        self.addLink(s2, s4, cls=TCLink, bw=10, delay='5ms', loss=0.5)

        # BAD path: s1 - s3 - s4  (low bandwidth, high delay, higher loss)
        self.addLink(s1, s3, cls=TCLink, bw=2, delay='40ms', loss=5)
        self.addLink(s3, s4, cls=TCLink, bw=2, delay='40ms', loss=5)


topos = {'qostopo': (lambda: QoSTopo())}


def run():
    setLogLevel('info')
    topo = QoSTopo()
    net = Mininet(
        topo=topo,
        switch=OVSSwitch,
        link=TCLink,
        controller=lambda name: RemoteController(name, ip='127.0.0.1', port=6633),
        autoSetMacs=True,
    )
    net.start()

    info('*** Testing connectivity\n')
    net.pingAll()

    info('*** Dropping into CLI. Try:\n')
    info('    h1 iperf -c h2 -t 10\n')
    info('    h1 ping h2\n')
    info('*** and watch the Ryu controller log to see which path it selects.\n')

    CLI(net)
    net.stop()


if __name__ == '__main__':
    run()
