# V1 Validation Notes

This document separates automated controller tests from live Mininet runtime evidence.

## 1. Automated unit and regression tests

The repository's existing validation record reports **39 passing pytest tests** during its maintenance pass. The suite uses Ryu import stubs and mocked datapaths. It covers controller logic and local message-dispatch behavior; it does not independently verify switch-side flow installation.

Run the suite with:

```bash
python -m pytest -v
```

## 2. Live Mininet connectivity and topology

The live demonstration recorded in [runtime-demo.md](runtime-demo.md) observed:

- Four Open vSwitch bridges (s1–s4) connected to the Ryu controller at `tcp:127.0.0.1:6653`.
- Mininet links connecting h1—s1, h2—s4, s1—s2—s4 and s1—s3—s4.
- `pingall` completed with 0% dropped (2/2 received).
- A later post-traffic 10-packet ping completed with 10/10 replies and 0% packet loss.

## 3. Active-flow rerouting

During TCP iperf traffic, the Ryu log recorded route changes between `[4, 2, 1]` and `[4, 3, 1]`. The controller logged computed cost changes and dispatch of forward and reverse flow-rule messages.

During one alternate-route interval, the s3 flow-table snapshot showed two priority-10 host-to-host forwarding entries with packet counters. This is separate switch-side evidence that those entries were present on s3 at the time of inspection.

## 4. Traffic measurements and interpretation

One iperf run reported 46.9 MBytes transferred over approximately 92.5 seconds at an aggregate 4.25 Mbits/sec. Per-interval throughput varied. This is a single observed run, not a controlled benchmark.

A five-packet ping issued while iperf was still active reported 20% packet loss. After iperf completed, a separate ten-packet ping reported 0% packet loss. These results are recorded separately.

No controlled baseline-versus-rerouted throughput or latency comparison was performed, so no performance improvement is claimed.

## 5. Link failure and recovery

A dedicated link-failure/recovery experiment is not documented by the current runtime evidence. Controller path changes under changing computed costs were observed, but they should not be described as a controlled physical link-failure test.

## 6. Limitations

- Delay, bandwidth, packet loss and jitter are configured profile values; they are not independently measured live by the controller.
- Utilization is estimated from OpenFlow port byte counters and configured link capacity.
- A successful `send_msg()` call confirms local message dispatch, not switch-side acceptance. The implementation does not use OpenFlow barriers or error replies to confirm every rule.
- Flow entries use `idle_timeout=60`; inspect them while relevant traffic is active.
- The pytest suite uses mocked datapaths and does not replace runtime testing with Ryu, Open vSwitch and Mininet.
