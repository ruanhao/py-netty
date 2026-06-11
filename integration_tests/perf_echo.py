#!/usr/bin/env python3
"""Local echo performance runner.

Usage examples:

    # Run all default local performance cases.
    python integration_tests/perf_echo.py --case all

    # Run a short latency smoke test and print JSON output.
    python integration_tests/perf_echo.py --case single_connection_latency --messages 20 --payload-size 64 --json

    # Compare py-netty, asyncio, and threaded socket implementations.
    python integration_tests/perf_echo.py --case all --engine all

    # Run a larger throughput case with custom concurrency and message count.
    python integration_tests/perf_echo.py --case large_payload_throughput --connections 32 --messages 64

    # Run a ramp-up case on an explicit localhost port.
    python integration_tests/perf_echo.py --case connection_ramp_up --port 19080 --connections 128

    # Run higher connection-count cases to find where threaded sockets fall behind.
    python integration_tests/perf_echo.py --case high_connection_scaling --engine all --timeout 30

Notes:
    The runner starts an in-process localhost echo server for each case and
    engine. Metrics are informational; the process exits non-zero only for
    functional failures such as missing echoes, connection failures, or
    timeouts.
"""
import argparse
import asyncio
import contextlib
import json
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from statistics import mean
from typing import Dict, List, Optional, Tuple

from py_netty import Bootstrap, EventLoopGroup, ServerBootstrap
from py_netty.channel import NioSocketChannel
from py_netty.handler import ChannelHandlerAdapter, EchoChannelHandler


HEADER = struct.Struct("!II")

USAGE_EXAMPLES = """examples:
  python integration_tests/perf_echo.py --case all
  python integration_tests/perf_echo.py --case single_connection_latency --messages 20 --payload-size 64 --json
  python integration_tests/perf_echo.py --case all --engine all
  python integration_tests/perf_echo.py --case large_payload_throughput --connections 32 --messages 64
  python integration_tests/perf_echo.py --case connection_ramp_up --port 19080 --connections 128
  python integration_tests/perf_echo.py --case high_connection_scaling --engine all --timeout 30

notes:
  Each case starts an in-process localhost echo server for each selected engine.
  Metrics are informational; only functional failures make the command fail.
"""


@dataclass(frozen=True)
class CaseSpec:
    name: str
    connections: int
    messages: int
    payload_size: int
    sequential: bool = False
    client_eventloops: int = 1


@dataclass
class CaseResult:
    engine: str
    case: str
    connections: int
    messages_per_connection: int
    payload_size: int
    sent_messages: int
    received_messages: int
    sent_bytes: int
    received_bytes: int
    elapsed_seconds: float
    ramp_up_seconds: float
    bytes_per_second: float
    messages_per_second: float
    latency_avg_ms: Optional[float]
    latency_p50_ms: Optional[float]
    latency_p95_ms: Optional[float]
    latency_p99_ms: Optional[float]
    errors: List[str]
    timed_out: bool


DEFAULT_CASES: Dict[str, CaseSpec] = {
    "single_connection_latency": CaseSpec(
        name="single_connection_latency",
        connections=1,
        messages=200,
        payload_size=64,
        sequential=True,
    ),
    "small_payload_concurrency": CaseSpec(
        name="small_payload_concurrency",
        connections=32,
        messages=200,
        payload_size=1024,
    ),
    "large_payload_throughput": CaseSpec(
        name="large_payload_throughput",
        connections=16,
        messages=32,
        payload_size=64 * 1024,
    ),
    "connection_ramp_up": CaseSpec(
        name="connection_ramp_up",
        connections=64,
        messages=1,
        payload_size=64,
    ),
    "backpressure_smoke": CaseSpec(
        name="backpressure_smoke",
        connections=8,
        messages=32,
        payload_size=64 * 1024,
    ),
}

HIGH_CONNECTION_CASES: Dict[str, CaseSpec] = {
    "high_connection_128": CaseSpec(
        name="high_connection_128",
        connections=128,
        messages=20,
        payload_size=1024,
    ),
    "high_connection_256": CaseSpec(
        name="high_connection_256",
        connections=256,
        messages=20,
        payload_size=1024,
    ),
    "high_connection_512": CaseSpec(
        name="high_connection_512",
        connections=512,
        messages=20,
        payload_size=1024,
    ),
}

CASE_GROUPS = {
    "all": tuple(DEFAULT_CASES),
    "high_connection_scaling": tuple(HIGH_CONNECTION_CASES),
}

ALL_CASES = {
    **DEFAULT_CASES,
    **HIGH_CONNECTION_CASES,
}


def _format_bytes_per_second(value: float) -> str:
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    size = float(value)
    unit = units[0]
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.2f} {unit}"


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * percentile))
    return ordered[index]


def _latency_ms(values: List[float], percentile: Optional[float] = None) -> Optional[float]:
    if not values:
        return None
    if percentile is None:
        return mean(values) * 1000
    value = _percentile(values, percentile)
    if value is None:
        return None
    return value * 1000


def _choose_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _make_frame(connection_id: int, sequence: int, payload_size: int) -> bytes:
    if payload_size < HEADER.size:
        raise ValueError(f"payload size must be at least {HEADER.size} bytes")
    filler = bytes([(connection_id + sequence) % 251]) * (payload_size - HEADER.size)
    return HEADER.pack(connection_id, sequence) + filler


def _validate_frame(frame: bytes, connection_id: int, expected_sequence: int) -> Optional[str]:
    received_connection_id, sequence = HEADER.unpack(frame[:HEADER.size])
    if received_connection_id != connection_id:
        return f"connection {connection_id}: expected id {connection_id}, received {received_connection_id}"
    if sequence != expected_sequence:
        return f"connection {connection_id}: expected sequence {expected_sequence}, received {sequence}"

    expected_fill = (connection_id + sequence) % 251
    payload = frame[HEADER.size:]
    if payload and payload != bytes([expected_fill]) * len(payload):
        return f"connection {connection_id}: payload mismatch at sequence {sequence}"
    return None


def _build_result(
    engine: str,
    spec: CaseSpec,
    run: "_RunState",
    sent_messages: int,
    sent_bytes: int,
    elapsed_seconds: float,
    ramp_up_seconds: float,
    timed_out: bool,
    timeout: float,
) -> CaseResult:
    errors = run.snapshot_errors()
    if timed_out:
        errors.append(
            f"timeout after {timeout}s: received {run.received_messages}/{sent_messages} messages"
        )

    if elapsed_seconds <= 0:
        elapsed_seconds = 0.000001

    return CaseResult(
        engine=engine,
        case=spec.name,
        connections=spec.connections,
        messages_per_connection=spec.messages,
        payload_size=spec.payload_size,
        sent_messages=sent_messages,
        received_messages=run.received_messages,
        sent_bytes=sent_bytes,
        received_bytes=run.received_bytes,
        elapsed_seconds=elapsed_seconds,
        ramp_up_seconds=ramp_up_seconds,
        bytes_per_second=run.received_bytes / elapsed_seconds,
        messages_per_second=run.received_messages / elapsed_seconds,
        latency_avg_ms=_latency_ms(run.latencies),
        latency_p50_ms=_latency_ms(run.latencies, 0.50),
        latency_p95_ms=_latency_ms(run.latencies, 0.95),
        latency_p99_ms=_latency_ms(run.latencies, 0.99),
        errors=errors,
        timed_out=timed_out,
    )


class _RunState:

    def __init__(self, total_connections: int):
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.total_connections = total_connections
        self.completed_connections = 0
        self.errors: List[str] = []
        self.received_messages = 0
        self.received_bytes = 0
        self.latencies: List[float] = []

    def record_message(self, size: int, latency: Optional[float]) -> None:
        with self._lock:
            self.received_messages += 1
            self.received_bytes += size
            if latency is not None:
                self.latencies.append(latency)

    def record_error(self, error: str) -> None:
        with self._lock:
            self.errors.append(error)
            self._done.set()

    def mark_connection_complete(self) -> None:
        with self._lock:
            self.completed_connections += 1
            if self.completed_connections == self.total_connections:
                self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def snapshot_errors(self) -> List[str]:
        with self._lock:
            return list(self.errors)


class _ClientState:

    def __init__(self, run: _RunState, connection_id: int, messages: int, payload_size: int, sequential: bool):
        self.run = run
        self.connection_id = connection_id
        self.messages = messages
        self.payload_size = payload_size
        self.sequential = sequential
        self.channel: Optional[NioSocketChannel] = None
        self.buffer = bytearray()
        self.sent_messages = 0
        self.received_messages = 0
        self.sent_at: Dict[int, float] = {}
        self.completed = False

    def send_next(self) -> None:
        if self.channel is None or self.sent_messages >= self.messages:
            return
        sequence = self.sent_messages
        self.sent_at[sequence] = time.perf_counter()
        self.channel.write(_make_frame(self.connection_id, sequence, self.payload_size))
        self.sent_messages += 1

    def send_all(self) -> None:
        while self.sent_messages < self.messages:
            self.send_next()

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        while len(self.buffer) >= self.payload_size:
            frame = bytes(self.buffer[:self.payload_size])
            del self.buffer[:self.payload_size]
            self._handle_frame(frame)

    def _handle_frame(self, frame: bytes) -> None:
        error = _validate_frame(frame, self.connection_id, self.received_messages)
        if error:
            self.run.record_error(error)
            return

        sequence = self.received_messages
        sent_at = self.sent_at.pop(sequence, None)
        latency = None if sent_at is None else time.perf_counter() - sent_at
        self.received_messages += 1
        self.run.record_message(len(frame), latency)

        if self.received_messages == self.messages and not self.completed:
            self.completed = True
            self.run.mark_connection_complete()
            return

        if self.sequential:
            self.send_next()


class _PerfClientHandler(ChannelHandlerAdapter):

    def __init__(self, state: _ClientState):
        super().__init__()
        self._state = state

    def channel_read(self, ctx, msg: bytes) -> None:
        self._state.feed(msg)

    def exception_caught(self, ctx, exception: Exception) -> None:
        self._state.run.record_error(f"connection {self._state.connection_id}: {exception}")
        ctx.close()


class _LocalEchoServer:

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port or _choose_free_port(host)
        self.parent_group = EventLoopGroup(1, "perf-acceptor")
        self.child_group = EventLoopGroup(1, "perf-worker")
        self.channel = None

    def __enter__(self):
        self.channel = ServerBootstrap(
            parent_group=self.parent_group,
            child_group=self.child_group,
            child_handler_initializer=EchoChannelHandler,
        ).bind(self.host, self.port).channel()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.channel is not None:
            self.channel.close(force=True)
            self.channel.close_future().sync()
        self.parent_group.__exit__(exc_type, exc_val, exc_tb)
        self.child_group.__exit__(exc_type, exc_val, exc_tb)


def _apply_overrides(spec: CaseSpec, args: argparse.Namespace) -> CaseSpec:
    return CaseSpec(
        name=spec.name,
        connections=args.connections or spec.connections,
        messages=args.messages or spec.messages,
        payload_size=args.payload_size or spec.payload_size,
        sequential=spec.sequential,
        client_eventloops=spec.client_eventloops,
    )


def _run_py_netty_case(spec: CaseSpec, host: str, port: int, timeout: float) -> CaseResult:
    if spec.payload_size < HEADER.size:
        raise ValueError(f"payload size must be at least {HEADER.size} bytes")

    run = _RunState(spec.connections)
    states: List[_ClientState] = [
        _ClientState(run, connection_id=i, messages=spec.messages, payload_size=spec.payload_size, sequential=spec.sequential)
        for i in range(spec.connections)
    ]
    channels = []
    sent_messages = spec.connections * spec.messages
    sent_bytes = sent_messages * spec.payload_size

    with _LocalEchoServer(host, port) as server:
        case_started = time.perf_counter()
        with EventLoopGroup(spec.client_eventloops, f"perf-client-{spec.name}") as client_group:
            bootstrap = Bootstrap(eventloop_group=client_group)
            for state in states:
                bootstrap.handler_initializer = lambda state=state: _PerfClientHandler(state)
                try:
                    channel = bootstrap.connect(server.host, server.port).sync().channel()
                except Exception as exc:
                    run.record_error(f"connection {state.connection_id}: connect failed: {exc}")
                    continue
                state.channel = channel
                channels.append(channel)

            ramp_up_seconds = time.perf_counter() - case_started
            send_started = time.perf_counter()

            for state in states:
                if state.channel is None:
                    continue
                if state.sequential:
                    state.send_next()
                else:
                    state.send_all()

            timed_out = not run.wait(timeout)
            elapsed_seconds = time.perf_counter() - send_started

            for channel in channels:
                try:
                    channel.close(force=True)
                    channel.close_future().sync()
                except Exception as exc:
                    run.record_error(f"close failed: {exc}")

    return _build_result(
        "py-netty",
        spec,
        run,
        sent_messages,
        sent_bytes,
        elapsed_seconds,
        ramp_up_seconds,
        timed_out,
        timeout,
    )


async def _asyncio_echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _asyncio_client(state: _ClientState, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        if state.sequential:
            for sequence in range(state.messages):
                started = time.perf_counter()
                writer.write(_make_frame(state.connection_id, sequence, state.payload_size))
                await writer.drain()
                frame = await reader.readexactly(state.payload_size)
                error = _validate_frame(frame, state.connection_id, sequence)
                if error:
                    state.run.record_error(error)
                    return
                state.run.record_message(len(frame), time.perf_counter() - started)
        else:
            sent_at = {}
            for sequence in range(state.messages):
                sent_at[sequence] = time.perf_counter()
                writer.write(_make_frame(state.connection_id, sequence, state.payload_size))
            await writer.drain()

            for sequence in range(state.messages):
                frame = await reader.readexactly(state.payload_size)
                error = _validate_frame(frame, state.connection_id, sequence)
                if error:
                    state.run.record_error(error)
                    return
                state.run.record_message(len(frame), time.perf_counter() - sent_at[sequence])

        state.run.mark_connection_complete()
    except Exception as exc:
        state.run.record_error(f"connection {state.connection_id}: {exc}")
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _run_asyncio_case_async(spec: CaseSpec, host: str, port: int, timeout: float) -> CaseResult:
    if spec.payload_size < HEADER.size:
        raise ValueError(f"payload size must be at least {HEADER.size} bytes")

    run = _RunState(spec.connections)
    sent_messages = spec.connections * spec.messages
    sent_bytes = sent_messages * spec.payload_size
    port = port or _choose_free_port(host)
    server = await asyncio.start_server(_asyncio_echo_handler, host, port, reuse_address=True)

    try:
        case_started = time.perf_counter()
        states = [
            _ClientState(run, connection_id=i, messages=spec.messages, payload_size=spec.payload_size, sequential=spec.sequential)
            for i in range(spec.connections)
        ]
        connections = await asyncio.gather(
            *(asyncio.open_connection(host, port) for _ in states),
            return_exceptions=True,
        )
        active_connections: List[Tuple[_ClientState, asyncio.StreamReader, asyncio.StreamWriter]] = []
        for state, connection in zip(states, connections):
            if isinstance(connection, Exception):
                run.record_error(f"connection {state.connection_id}: connect failed: {connection}")
                continue
            reader, writer = connection
            active_connections.append((state, reader, writer))
        ramp_up_seconds = time.perf_counter() - case_started
        send_started = time.perf_counter()

        tasks = [
            asyncio.create_task(_asyncio_client(state, reader, writer))
            for state, reader, writer in active_connections
        ]
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
                timed_out = False
            except asyncio.TimeoutError:
                timed_out = True
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        else:
            timed_out = True

        elapsed_seconds = time.perf_counter() - send_started
    finally:
        server.close()
        await server.wait_closed()

    return _build_result(
        "asyncio",
        spec,
        run,
        sent_messages,
        sent_bytes,
        elapsed_seconds,
        ramp_up_seconds,
        timed_out,
        timeout,
    )


def _run_asyncio_case(spec: CaseSpec, host: str, port: int, timeout: float) -> CaseResult:
    return asyncio.run(_run_asyncio_case_async(spec, host, port, timeout))


class _ThreadedEchoServer:

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port or _choose_free_port(host)
        self.stop_event = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen()
        self.sock.settimeout(0.2)
        self.threads: List[threading.Thread] = []
        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)

    def __enter__(self):
        self.accept_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop_event.set()
        with contextlib.suppress(Exception):
            self.sock.close()
        self.accept_thread.join(timeout=1)
        for thread in self.threads:
            thread.join(timeout=1)

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_client, args=(client,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _handle_client(self, client: socket.socket) -> None:
        with client:
            while not self.stop_event.is_set():
                try:
                    data = client.recv(64 * 1024)
                except OSError:
                    break
                if not data:
                    break
                try:
                    client.sendall(data)
                except OSError:
                    break


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError(f"socket closed with {remaining} bytes remaining")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _threaded_connect(host: str, port: int, timeout: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return sock


def _threaded_client(state: _ClientState, sock: socket.socket) -> None:
    try:
        with sock:
            if state.sequential:
                for sequence in range(state.messages):
                    started = time.perf_counter()
                    sock.sendall(_make_frame(state.connection_id, sequence, state.payload_size))
                    frame = _recv_exact(sock, state.payload_size)
                    error = _validate_frame(frame, state.connection_id, sequence)
                    if error:
                        state.run.record_error(error)
                        return
                    state.run.record_message(len(frame), time.perf_counter() - started)
            else:
                sent_at = {}
                for sequence in range(state.messages):
                    sent_at[sequence] = time.perf_counter()
                    sock.sendall(_make_frame(state.connection_id, sequence, state.payload_size))
                for sequence in range(state.messages):
                    frame = _recv_exact(sock, state.payload_size)
                    error = _validate_frame(frame, state.connection_id, sequence)
                    if error:
                        state.run.record_error(error)
                        return
                    state.run.record_message(len(frame), time.perf_counter() - sent_at[sequence])
            state.run.mark_connection_complete()
    except Exception as exc:
        state.run.record_error(f"connection {state.connection_id}: {exc}")


def _run_threaded_case(spec: CaseSpec, host: str, port: int, timeout: float) -> CaseResult:
    if spec.payload_size < HEADER.size:
        raise ValueError(f"payload size must be at least {HEADER.size} bytes")

    run = _RunState(spec.connections)
    sent_messages = spec.connections * spec.messages
    sent_bytes = sent_messages * spec.payload_size

    with _ThreadedEchoServer(host, port) as server:
        states = [
            _ClientState(run, connection_id=i, messages=spec.messages, payload_size=spec.payload_size, sequential=spec.sequential)
            for i in range(spec.connections)
        ]
        case_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=spec.connections, thread_name_prefix=f"perf-threaded-{spec.name}") as pool:
            connect_futures = {
                pool.submit(_threaded_connect, host, server.port, timeout): state
                for state in states
            }
            active_connections: List[Tuple[_ClientState, socket.socket]] = []
            timed_out = False
            try:
                for future in as_completed(connect_futures, timeout=timeout):
                    state = connect_futures[future]
                    try:
                        active_connections.append((state, future.result()))
                    except Exception as exc:
                        run.record_error(f"connection {state.connection_id}: connect failed: {exc}")
            except TimeoutError:
                timed_out = True
            ramp_up_seconds = time.perf_counter() - case_started

            send_started = time.perf_counter()
            futures = [
                pool.submit(_threaded_client, state, sock)
                for state, sock in active_connections
            ]
            try:
                for future in as_completed(futures, timeout=timeout):
                    with contextlib.suppress(Exception):
                        future.result()
            except TimeoutError:
                timed_out = True
            timed_out = timed_out or not run.wait(0)
            elapsed_seconds = time.perf_counter() - send_started

    return _build_result(
        "threaded",
        spec,
        run,
        sent_messages,
        sent_bytes,
        elapsed_seconds,
        ramp_up_seconds,
        timed_out,
        timeout,
    )


def _run_case(spec: CaseSpec, engine: str, host: str, port: int, timeout: float) -> CaseResult:
    if engine == "py-netty":
        return _run_py_netty_case(spec, host, port, timeout)
    if engine == "asyncio":
        return _run_asyncio_case(spec, host, port, timeout)
    if engine == "threaded":
        return _run_threaded_case(spec, host, port, timeout)
    raise ValueError(f"unsupported engine: {engine}")


def _print_result(result: CaseResult) -> None:
    status = "FAIL" if result.errors or result.timed_out else "OK"
    print(f"[{status}] {result.engine} / {result.case}")
    print(f"  connections: {result.connections}")
    print(f"  messages:    {result.received_messages}/{result.sent_messages}")
    print(f"  payload:     {result.payload_size} bytes")
    print(f"  elapsed:     {result.elapsed_seconds:.3f}s")
    print(f"  ramp-up:     {result.ramp_up_seconds:.3f}s")
    print(f"  throughput:  {_format_bytes_per_second(result.bytes_per_second)}")
    print(f"  msg rate:    {result.messages_per_second:.2f} msg/s")
    if result.latency_avg_ms is not None:
        print(
            "  latency:     "
            f"avg={result.latency_avg_ms:.3f}ms "
            f"p50={result.latency_p50_ms:.3f}ms "
            f"p95={result.latency_p95_ms:.3f}ms "
            f"p99={result.latency_p99_ms:.3f}ms"
        )
    if result.errors:
        print("  errors:")
        for error in result.errors:
            print(f"    - {error}")


def _parse_args() -> argparse.Namespace:
    case_names = list(CASE_GROUPS) + list(ALL_CASES)
    engine_names = ["all", "py-netty", "asyncio", "threaded"]
    parser = argparse.ArgumentParser(
        description="Run local echo performance cases.",
        epilog=USAGE_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--case", choices=case_names, default="all")
    parser.add_argument("--engine", choices=engine_names, default="py-netty")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="Use 0 to auto-pick a free localhost port.")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--connections", type=int, default=None)
    parser.add_argument("--messages", type=int, default=None)
    parser.add_argument("--payload-size", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON results.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.case in CASE_GROUPS:
        selected = [ALL_CASES[name] for name in CASE_GROUPS[args.case]]
    else:
        selected = [ALL_CASES[args.case]]
    engines = ["py-netty", "asyncio", "threaded"] if args.engine == "all" else [args.engine]
    results = []

    for base_spec in selected:
        for engine in engines:
            spec = _apply_overrides(base_spec, args)
            result = _run_case(spec, engine, args.host, args.port, args.timeout)
            results.append(result)
            if not args.json:
                _print_result(result)

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2, sort_keys=True))

    return 1 if any(result.errors or result.timed_out for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
