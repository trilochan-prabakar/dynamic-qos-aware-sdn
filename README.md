# Dynamic QoS-Aware Path Selection Using SDN

A refined SDN implementation using Ryu, OpenFlow 1.3, Mininet and Open vSwitch. The controller selects and re-evaluates paths through a four-switch diamond topology using configured QoS profiles and utilization estimated from OpenFlow port counters.

> **Project origin and attribution:** This work began as a group assignment. The original project and contributor history are preserved in [the original repository](https://github.com/taniishraj/dynamic-qos-aware-sdn). This repository documents subsequent improvements and validation; it does not claim sole authorship of the original assignment.

## Overview

The controller learns host locations, discovers switch links, installs forwarding rules, polls port statistics and periodically re-evaluates active flows.

Two routes connect the host-facing switches:

- **Preferred route:** s1 → s2 → s4 — 10 Mbps links, 5 ms configured delay, 0.5% configured loss.
- **Alternate route:** s1 → s3 → s4 — 2 Mbps links, 40 ms configured delay, 5% configured loss.

## Features

- Ryu topology discovery and host learning.
- OpenFlow 1.3 forwarding rules.
- Loop-free spanning-tree handling for ARP and broadcast traffic.
- QoS scoring from configured delay, bandwidth, loss and jitter profiles, plus utilization estimated from OpenFlow port byte counters.
- NetworkX shortest-path selection using summed reciprocal link scores.
- Periodic active-flow re-optimization; rerouting occurs when the prior path is invalid or a candidate has strictly more than 15% lower aggregate cost.
- Defensive handling of invalid/missing link metrics and failed local flow-message dispatch.

### Important implementation details

- Delay, bandwidth, packet-loss and jitter are configured profile inputs; they are **not independently measured live** by the controller.
- Port byte counters are sampled to estimate utilization.
- The monitoring interval is 10 seconds.
- A successful `send_msg()` call confirms local message dispatch, not switch-side acceptance. The implementation does not use OpenFlow barriers or error replies to confirm installation.
- Flow entries use an idle timeout; an unused entry can disappear after 60 seconds.

## Architecture

```text
h1 ── s1 ── s2 ── s4 ── h2
       \           /
        └── s3 ───┘

Ryu controller
 ├── topology discovery
 ├── host learning and packet handling
 ├── QoS scoring and shortest-path selection
 ├── port-statistics polling
 └── active-flow re-optimization
          │
          └── OpenFlow 1.3 → Open vSwitch
```

## Technologies

- Ubuntu 24.04
- Python 3.11.18
- Ryu 4.34
- Eventlet 0.41.3
- Mininet 2.3.0
- Open vSwitch 3.3.9
- NetworkX 3.6.1

## Repository layout

- `qos_controller.py` — controller, QoS scoring, route selection, telemetry and flow handling.
- `topology.py` — Mininet diamond topology and link-emulation settings.
- `tests/` — controller unit and regression tests.
- `requirements.txt` — runtime Python dependencies.
- `requirements-dev.txt` — test dependencies.
- `docs/validation.md` — automated test notes and validation boundaries.
- `docs/runtime-demo.md` — observed Mininet/Ryu runtime demonstration and evidence index.
- `docs/troubleshooting.md` — environment and startup troubleshooting.

## Setup

The runtime demonstration was performed on Ubuntu 24.04 with Python 3.11.18. Use a Python 3.11 virtual environment for Ryu; do not use the old Eventlet 0.30.2 pin with this environment.

Install system packages (package names can vary slightly by Ubuntu release):

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip \
  mininet openvswitch-switch openvswitch-common git
sudo systemctl enable --now openvswitch-switch
```

From the repository root:

```bash
python3.11 -m venv ~/sdn-lab/ryu-env311
source ~/sdn-lab/ryu-env311/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

Confirm the environment:

```bash
python --version
python -m pip show ryu eventlet networkx
```

The demonstrated compatible versions were Python 3.11.18, Ryu 4.34, Eventlet 0.41.3 and NetworkX 3.6.1.

## Run the live topology

Use two terminals. In **Terminal 1**, activate the Python 3.11 environment, change to the repository root, and start Ryu:

```bash
source ~/sdn-lab/ryu-env311/bin/activate
cd /path/to/dynamic-qos-aware-sdn
ryu-manager --observe-links qos_controller.py
```

In **Terminal 2**, clean up stale Mininet state and start the topology against the Ryu OpenFlow listener on port 6653:

```bash
sudo mn -c
sudo mn --custom topology.py --topo qostopo \
  --controller remote,ip=127.0.0.1,port=6653 \
  --switch ovs,protocols=OpenFlow13
```

Wait for all four switches to connect and topology discovery to complete. At the `mininet>` prompt, useful checks include:

```text
pingall
net
sh ovs-vsctl show
sh ovs-ofctl -O OpenFlow13 dump-flows s1
sh ovs-ofctl -O OpenFlow13 dump-flows s3
```

For traffic generation, start an iperf server on h2 and a client on h1:

```text
h2 iperf -s &
h1 iperf -c 10.0.0.2 -t 90 -i 5
```

Watch the Ryu terminal for path selection and re-routing messages. Flow entries have an idle timeout of 60 seconds, so inspect a switch's flow table while the relevant traffic is active.

**Port note:** the standalone `run()` helper in `topology.py` specifies port 6633, while the verified CLI command above explicitly uses 6653. Use the command shown here for the demonstrated setup unless you intentionally change the controller listener and topology configuration together.

## Validation

See [the automated validation notes](docs/validation.md) and [the observed runtime demonstration](docs/runtime-demo.md). These are separate evidence types: mocked unit tests validate controller logic, while the runtime notes document observed Mininet, OVS and Ryu output.

## Limitations

- This is a Mininet lab implementation, not a production SDN controller.
- Delay, bandwidth, loss and jitter are configured profile values rather than live measurements.
- Utilization is estimated from port byte counters and configured link capacity.
- Local `send_msg()` dispatch does not prove switch-side acceptance.
- Runtime results depend on topology convergence, OpenFlow message handling and the virtualized host environment.

## Attribution

The project originated as a Computer Networks group assignment. The original project is available at [taniishraj/dynamic-qos-aware-sdn](https://github.com/taniishraj/dynamic-qos-aware-sdn). Original authorship and contributor history are acknowledged; refinements and additional validation are documented in this repository.
