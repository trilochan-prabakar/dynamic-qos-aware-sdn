# V1 Validation Notes

These notes separate automated controller tests from runtime network checks. The
runtime checks below were not performed during the maintenance pass. Earlier
README text reported Mininet and traffic observations, but the repository does
not contain their command output, logs, or result files, so those observations
are not presented here as verified results.

## 1. Automated unit and regression tests

The complete pytest suite passed **39 tests** during this maintenance pass.
The suite covers route selection and cost validation, monitoring-loop resilience,
flow-rule dispatch reporting, known-unicast fallback behavior, and reroute
failure handling. The tests use Ryu import stubs and mocked datapaths. They
verify controller logic and local message-dispatch behavior; they do not verify
switch-side flow installation.

## 2. Mininet connectivity

Not run during this maintenance pass. The earlier README reported that
`pingall` completed with no packet loss after controller and topology
convergence, but supporting runtime output is not checked into this repository.

## 3. Congestion-triggered rerouting

Not run in Mininet during this maintenance pass. The unit suite checks route
selection and the configured strict 15% aggregate-cost improvement threshold.
Those tests do not establish that a live switch rerouted traffic under load.

## 4. Link failure and recovery

Not run in Mininet during this maintenance pass. The earlier README described
failover to the alternate route and return after link recovery, but no switch
state or connectivity output is available in the repository to independently
verify that runtime result.

## 5. TCP and UDP traffic experiments

Not run during this maintenance pass. The earlier README stated that TCP/UDP
iperf runs were exercised and noted UDP loss and out-of-order datagrams under
load. No raw traffic logs or measurements are checked in, so no throughput,
latency, loss, or jitter result is claimed here.

## 6. Limitations and interpretation

- Delay, bandwidth, packet loss, and jitter are configured QoS profile values;
  they are not measured live by the controller. Utilization is estimated from
  OpenFlow port byte counters and configured link capacity.
- A successful `send_msg()` call means the controller dispatched a message
  locally. The implementation does not use OpenFlow barriers or error replies
  to confirm that switches accepted or installed the rule.
- During active-flow rerouting, the controller updates its recorded forward and
  reverse paths only after both directional path dispatches complete. A
  multi-switch path can be partially dispatched if a later datapath fails. The
  controller logs the number of messages dispatched; it cannot roll back rules
  that may already have been accepted by switches.
- The pytest suite uses mocked Ryu datapaths. It is not a substitute for
  connectivity, congestion, link failure/recovery, or TCP/UDP tests with Ryu,
  Open vSwitch, and Mininet.
