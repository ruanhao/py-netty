---
name: perf-echo
description: Use when updating, validating, regenerating, or documenting py-netty perf_echo benchmarks, charts, README benchmark data, or local echo performance comparisons against asyncio and threaded sockets.
---

# perf_echo Benchmark Workflow Skill

Use this workflow when updating, validating, or regenerating the local echo
performance benchmark for py-netty.

## Goal

Produce reproducible local benchmark data for py-netty echo traffic, optionally
compare it with Python standard-library implementations, generate charts, and
update README benchmark documentation.

## Benchmark Runner

Primary command:

```bash
python integration_tests/perf_echo.py --case all --engine all --timeout 20 --json
```

Useful smoke command:

```bash
python integration_tests/perf_echo.py --case single_connection_latency --messages 3 --payload-size 32 --timeout 5 --engine all
```

Supported engines:

- `py-netty`
- `asyncio`
- `threaded`
- `all`

Supported cases:

- `single_connection_latency`
- `backpressure_smoke`
- `large_payload_throughput`
- `small_payload_concurrency`
- `connection_ramp_up`
- `all`

## Test Principle

Each case starts a local in-process echo server for the selected engine and
creates matching clients. Clients send fixed-size framed payloads. The first
8 bytes encode:

- `connection_id`
- `sequence`

The remaining bytes are deterministic filler. The client validates every echoed
frame to handle TCP split/coalesced reads correctly.

The run fails only for functional errors:

- connection failures
- missing echoes
- payload mismatches
- sequence mismatches
- timeouts

Throughput and latency values are informational and environment-dependent.

## Data Collection

Save benchmark JSON outside the repo unless the user explicitly asks to commit
raw data:

```bash
python integration_tests/perf_echo.py --case all --engine all --timeout 20 --json > /tmp/py-netty-perf-compare.json
```

Before using the data, confirm every result has:

- empty `errors`
- `timed_out` set to `false`
- `received_messages == sent_messages`
- `received_bytes == sent_bytes`

## Chart Generation

Generate charts from `/tmp/py-netty-perf-compare.json` into `img/`.

Required chart files:

- `img/perf_echo_throughput.png`
- `img/perf_echo_message_rate.png`
- `img/perf_echo_latency.png`
- `img/perf_echo_ramp_up.png`

Chart requirements:

- Sort cases by connection count ascending.
- Use grouped bars for `py-netty`, `asyncio`, and `threaded` when comparison
  data is available.
- Label axes with units.
- Keep filenames stable so README links do not churn.

Recommended metric mapping:

- Throughput: `bytes_per_second / 1024 / 1024`, unit `MiB/s`.
- Message rate: `messages_per_second`, unit `msg/s`.
- Latency: `latency_p95_ms`, unit `ms`.
- Ramp-up: `ramp_up_seconds * 1000`, unit `ms`.

## README Update

Update the `## Benchmark` section in `README.md`.

The benchmark section should include:

- the exact command used to collect data
- local environment summary
- a table with case, engine, connections, payload, messages, throughput,
  message rate, p50 latency, p95 latency, and ramp-up
- chart links to the four `img/perf_echo_*.png` files
- a note that metrics are informational and environment-dependent
- a note that benchmark failures are functional failures, not performance
  threshold failures

Keep the table ordered by connection count:

1. `single_connection_latency`
2. `backpressure_smoke`
3. `large_payload_throughput`
4. `small_payload_concurrency`
5. `connection_ramp_up`

For each case, list engines in this order:

1. `py-netty`
2. `asyncio`
3. `threaded`

## Validation

Run these checks after modifying the benchmark runner, charts, or README:

```bash
python integration_tests/perf_echo.py --help
python integration_tests/perf_echo.py --case single_connection_latency --messages 3 --payload-size 32 --timeout 5 --engine all
pytest -q
```

Validate chart files are readable and non-empty:

```bash
python - <<'PY'
from pathlib import Path
from PIL import Image

for path in sorted(Path("img").glob("perf_echo_*.png")):
    with Image.open(path) as image:
        print(path, image.size, image.mode)
PY
```

## Cautions

- Do not add these benchmark cases to default `pytest`; they are local
  integration/performance checks.
- Do not commit `/tmp` raw output files.
- Do not use hard performance thresholds unless the user explicitly asks.
- Do not compare results from different machines as absolute performance
  claims.
- If a high-connection case fails with `Too many open files`, lower
  `--connections` or raise the OS file descriptor limit before rerunning.
