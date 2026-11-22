# Mininet/Ryu Runtime Demonstration

This document records the observed live demonstration on Ubuntu 24.04 using Python 3.11.18, Ryu 4.34, Eventlet 0.41.3, Mininet 2.3.0 and Open vSwitch 3.3.9.

## Topology and controller connectivity

- Hosts: h1 (10.0.0.1/24) and h2 (10.0.0.2/24).
- Switches: s1, s2, s3 and s4.
- Host links: h1—s1 and h2—s4.
- Candidate routes: s1—s2—s4 and s1—s3—s4.
- `ovs-vsctl show` reported `is_connected: true` for all four bridges to `tcp:127.0.0.1:6653`.
- `net` showed both inter-switch paths and the host-facing links.
- `pingall` reported 0% dropped (2/2 received).

## Traffic and rerouting observations

An iperf TCP client from h1 to h2 ran for approximately 92.5 seconds. The reported aggregate transfer was 46.9 MBytes at 4.25 Mbits/sec. Per-interval throughput varied during the run; this is an observation from one lab run, not a benchmark or a comparison between routes.

The Ryu log recorded the following path changes for the active host pair:

1. Forward path `[1, 2, 4]` (reverse `[4, 2, 1]`) was initially selected.
2. The controller logged a re-route from `[4, 2, 1]` to `[4, 3, 1]`, with aggregate cost changing from 4.51732 to 3.58391.
3. It later logged a return from `[4, 3, 1]` to `[4, 2, 1]`, with aggregate cost changing from 4.33759 to 2.44416.
4. During a subsequent traffic run, it again logged a change from `[4, 2, 1]` to `[4, 3, 1]`, with aggregate cost changing from 4.37687 to 3.58392.

These are controller log observations. They demonstrate that the controller's active-flow re-optimization selected both candidate routes under changing computed costs.

## Switch flow-table observation

While the controller had selected the alternate route and traffic was active, `ovs-ofctl -O OpenFlow13 dump-flows s3` showed two priority-10 host-to-host forwarding entries:

- h2-to-h1 match, output `s3-eth1`, 317 packets.
- h1-to-h2 match, output `s3-eth2`, 355 packets.

This is switch flow-table evidence that the alternate path rules were present on s3 at the time of inspection. The entries had `idle_timeout=60`.

## Connectivity after the traffic run

A subsequent 10-packet ping from h1 to h2 reported:

- 10 transmitted, 10 received.
- 0% packet loss.
- Average RTT 37.762 ms; minimum 27.031 ms; maximum 110.223 ms.

An earlier 5-packet ping executed while iperf was still active reported 20% packet loss. After iperf completed, the 10-packet test reported no loss. These results should not be conflated: the clean 10-packet result is the post-traffic check.

## Evidence screenshots

The accompanying screenshots are intended to be stored in `docs/images/` with these names:

- `all-switches-controller-connections.png` — controller address for s1–s4.
- `mininet-topology-connections.png` — Mininet `net` output.
- `connectivity.png` — initial `pingall` result.
- `controller-flows.png` — s1 forwarding flow entries.
- `dynamic-rerouting-controller.png` — Ryu rerouting log.
- `alternate-path-s3-flows.png` — alternate-path forwarding entries on s3.
- `final-connectivity.png` — post-traffic 10/10 ping result.

## Interpretation and caveats

The run provides evidence of controller-triggered path changes, alternate-path flow entries on s3, and successful post-traffic connectivity. It does **not** establish that the alternate route improves throughput or latency: no controlled baseline-versus-rerouted benchmark was performed.

The configured delay, bandwidth, loss and jitter inputs are not live measurements. Utilization is estimated from port byte counters. The controller's dispatch logs report local OpenFlow message dispatch; the s3 flow-table snapshot provides separate switch-side evidence for the alternate path at one point in time. The controller does not use barriers or error replies to confirm every switch's acceptance of every rule.
