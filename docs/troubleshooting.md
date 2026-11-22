# Troubleshooting the V1 Mininet/Ryu Lab

## Ryu crashes with an Eventlet/Python compatibility error

The initial environment used Ryu 4.34 with Eventlet 0.30.2 on a newer Python runtime. That combination failed during startup because the old Eventlet version imports the removed Python `imp` module.

**Working environment observed in this lab:**

- Python 3.11.18
- Ryu 4.34
- Eventlet 0.41.3
- NetworkX 3.6.1

Create and activate a dedicated Python 3.11 virtual environment, then install the versions pinned in `requirements.txt`. Do not reuse the old environment containing Eventlet 0.30.2.

## Mininet reports that the remote controller is unavailable

1. Start Ryu first and leave its terminal running.
2. Confirm Ryu has started without exiting.
3. Start Mininet with `--controller remote,ip=127.0.0.1,port=6653`.
4. Wait for all four switches to connect; the Ryu terminal should report switch connections and topology discovery.

The verified command uses port 6653. The optional standalone `run()` helper in `topology.py` currently specifies 6633, so do not mix the helper's setting with the CLI command shown in the README.

## Clean up stale Mininet state

If a previous Mininet run was interrupted, run:

```bash
sudo mn -c
```

Then start the topology again.

## Flow entries are missing when inspected

The controller installs host forwarding entries with `idle_timeout=60`. An entry can expire after 60 seconds without matching traffic. Generate traffic and inspect the relevant switch immediately:

```text
h1 ping -c 3 h2
sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

For alternate-path verification, inspect s3 while the controller has selected `[4, 3, 1]` and traffic is active:

```text
sh ovs-ofctl -O OpenFlow13 dump-flows s3
```

An empty s3 host-flow table after the controller returns to the s2 path, or after entries have been idle, does not by itself prove that the earlier alternate-path dispatch never happened.

## iperf output appears mixed with another command

If an iperf client is still running in the background, its interval output may appear while other Mininet commands are entered. Wait for the client to finish before taking a clean connectivity measurement. For a post-load check:

```text
h1 ping -c 10 h2
```

## OVS controller output includes ptcp ports

`ovs-vsctl get-controller s1` may show both a passive management listener such as `ptcp:6654` and the active Ryu connection `tcp:127.0.0.1:6653`. The `ptcp` listener is separate from the Ryu controller connection.
