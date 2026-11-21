# Dynamic QoS-Aware Path Selection Using SDN

A Computer Networks group project refined into a tested SDN implementation using Ryu, OpenFlow 1.3, Mininet and Open vSwitch.

> **Project origin and attribution:** This work began as a group assignment. The original project and contributor history are preserved in [the original repository](https://github.com/taniishraj/dynamic-qos-aware-sdn). This repository documents subsequent improvements and regression testing; it does not claim sole authorship of the original assignment.

## Overview

The controller selects paths through a four-switch Mininet diamond topology. It learns host locations, discovers switch links, installs forwarding rules, polls OpenFlow port statistics, and periodically re-evaluates active flows.

The topology provides two routes between the host-facing switches:

- Preferred route: s1 → s2 → s4 — 10 Mbps links, 5 ms configured delay, 0.5% configured loss.
- Alternate route: s1 → s3 → s4 — 2 Mbps links, 40 ms configured delay, 5% configured loss.

## What the implementation does

- Discovers switches and inter-switch links through Ryu's topology API.
- Learns host locations and installs OpenFlow 1.3 forwarding entries.
- Builds a loop-free spanning tree for ARP and broadcast flooding.
- Scores links using configured delay, bandwidth, loss and jitter profiles, plus utilization estimated from OpenFlow port byte counters.
- Selects routes with NetworkX shortest-path calculation over summed reciprocal link scores.
- Re-evaluates active flows and reroutes when the previous path is invalid or the new path has **strictly more than 15% lower aggregate cost**.
- Validates path costs and handles invalid or missing link metrics without treating malformed links as usable routes.
- Updates recorded forward/reverse flow state only when the required flow-rule messages can be dispatched.

### Important implementation details

- Delay, bandwidth, packet-loss and jitter values are configured QoS profile inputs; they are **not independently measured live** by the controller.
- Port byte counters are sampled periodically to estimate link utilization.
- The monitoring interval is 10 seconds.
- A successful send_msg() call confirms local message dispatch, not a switch-side OpenFlow barrier acknowledgement. Runtime flow-table checks are used as separate evidence.

## Architecture

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
              └── OpenFlow 1.3 rules → Open vSwitch

## Technologies

- Python 3.11
- Ryu 4.34
- OpenFlow 1.3
- Mininet 2.3.0
- Open vSwitch 3.3.9
- NetworkX
- pytest

## Repository layout

- qos_controller.py — controller, QoS scoring, route selection, telemetry and flow handling.
- topology.py — Mininet diamond topology and link emulation settings.
- tests/ — controller unit and regression tests.
- requirements.txt — runtime dependencies.
- requirements-dev.txt — test dependencies.
- docs/validation.md — automated and Mininet runtime validation notes.
- Dynamic_QoS_SDN_Report_Reformatted.pdf — original project report; read the implementation-scope note in this README when comparing the report with the refined code.

## Setup

The following environment was used for validation: Ubuntu 24.04.2, Python 3.11.18, Ryu 4.34, Mininet 2.3.0 and Open vSwitch 3.3.9.

Install system prerequisites for Mininet and Open vSwitch using your distribution's package manager. Then create a Python virtual environment and install the project dependencies:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    python -m pip install -r requirements-dev.txt

Start the controller in one terminal:

    ryu-manager --verbose --observe-links qos_controller.py

Start the topology in another terminal:

    sudo mn --custom topology.py --topo qostopo \
      --controller remote,ip=127.0.0.1,port=6633 \
      --switch ovs,protocols=OpenFlow13

Run the automated tests from the project root:

    python -m pytest -v

## Validation

- **Automated tests:** 31 passed in the validated Ubuntu VM.
- **Connectivity:** after topology and controller convergence, pingall completed with 0% packet loss.
- **Congestion response:** controller logs showed active-flow route changes between the preferred and alternate paths under changing load.
- **Link failure and recovery:** taking the preferred link down caused traffic to use the alternate route; restoring the link allowed the controller to return to the preferred route. Switch forwarding entries and end-to-end connectivity were checked.
- **Traffic tests:** TCP/UDP iperf tests were exercised. Individual UDP runs showed loss and out-of-order datagrams under load; these observations are not presented as guaranteed throughput or QoS improvements.

See the validation notes for the scope and interpretation of these checks.

## Limitations

- This is a Mininet lab implementation, not a production SDN controller.
- Delay, bandwidth, loss and jitter are configured profile values rather than live measurements.
- Utilization is estimated from port byte counters and the configured link capacity.
- Runtime behaviour depends on topology convergence, OpenFlow message handling and the virtualized host environment.
- The test suite uses mocked datapaths for controller logic; Mininet runtime checks are separate.

## Attribution

The project originated as a Computer Networks group assignment. The original project is available at [taniishraj/dynamic-qos-aware-sdn](https://github.com/taniishraj/dynamic-qos-aware-sdn). Original authorship and contributor history are acknowledged; the refinements and additional regression tests are documented in this repository.
