# perf_echo.py

`perf_echo.py` is a standalone local performance runner for echo traffic. It can
run the same framed echo cases against py-netty, Python `asyncio`, and blocking
sockets with one thread per connection (`threaded`). It starts an in-process
local echo server for each selected engine, creates matching clients, sends
fixed-size framed payloads, validates echoed responses, and prints performance
metrics.

## How It Works

Each case starts a local echo server for the selected engine. The py-netty
engine uses `ServerBootstrap` and `EchoChannelHandler`; the comparison engines
use standard-library `asyncio` streams or blocking sockets. Every server echoes
received bytes back to the client.

Clients send fixed-size frames. The first 8 bytes are:

- `connection_id`
- `sequence`

The remaining bytes are deterministic payload filler. This framing lets the
client reconstruct messages even when TCP packets are split or coalesced.

The client handler buffers inbound data, slices complete frames, and verifies:

- the echoed `connection_id`
- the expected `sequence`
- the payload contents
- the total number of received messages

## Metrics

The runner reports:

- sent and received message counts
- sent and received bytes
- elapsed time
- connection ramp-up time
- bytes per second
- messages per second
- latency average, p50, p95, and p99
- errors and timeout status

Metrics are informational. The runner does not enforce fixed throughput or
latency thresholds.

## Cases

- `single_connection_latency`: one connection, sequential request/response
  traffic, focused on round-trip latency.
- `small_payload_concurrency`: many concurrent connections with small payloads,
  focused on event-loop scheduling and aggregate message throughput.
- `large_payload_throughput`: concurrent connections with large payloads,
  focused on byte throughput.
- `connection_ramp_up`: many short-lived connections with one echo each,
  focused on connection activation and ramp-up time.
- `backpressure_smoke`: queued large writes across several connections, focused
  on pending-write and backpressure paths.

## Usage

Run all default cases with py-netty:

```bash
python integration_tests/perf_echo.py --case all
```

Compare py-netty, asyncio, and threaded sockets:

```bash
python integration_tests/perf_echo.py --case all --engine all
```

Run a short latency case with JSON output:

```bash
python integration_tests/perf_echo.py --case single_connection_latency --messages 20 --payload-size 64 --engine all --json
```

Run a larger throughput case:

```bash
python integration_tests/perf_echo.py --case large_payload_throughput --connections 32 --messages 64
```

Run a ramp-up case on an explicit local port:

```bash
python integration_tests/perf_echo.py --case connection_ramp_up --port 19080 --connections 128
```

## Pass And Fail Semantics

The process exits with status `0` when all expected echoes are received and
validated.

The process exits non-zero for functional failures, including:

- connection failures
- missing echoes
- payload mismatches
- unexpected sequence numbers
- timeouts

Performance numbers alone do not fail the run.
