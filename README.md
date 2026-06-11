# py-netty :rocket:

[![CI](https://github.com/ruanhao/py-netty/actions/workflows/ci.yml/badge.svg)](https://github.com/ruanhao/py-netty/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/ruanhao/py-netty/branch/main/graph/badge.svg)](https://codecov.io/gh/ruanhao/py-netty)
[![Downloads](https://static.pepy.tech/badge/py-netty)](https://pepy.tech/project/py-netty)

An event-driven TCP networking framework.

Ideas and concepts under the hood are built upon those of [Netty](https://netty.io/), especially the IO and executor model.

APIs are designed to feel familiar to Netty users.


# Features

- callback based application invocation
- non blocking IO
- recv/write is performed only in IO thread
- adaptive read buffer 
- low/higher water mark to indicate writability (default low water mark is 32K and high water mark is 64K)
- all platform supported (linux: epoll, mac: kqueue, windows: select)

## Installation

```bash
pip install py-netty
```

## Getting Started

Start an echo server:

```python
from py_netty import ServerBootstrap
ServerBootstrap().bind(address='0.0.0.0', port=8080).close_future().sync()
```

Start an echo server (TLS):

```python
from py_netty import ServerBootstrap
ServerBootstrap(certfile='/path/to/cert/file', keyfile='/path/to/key/file').bind(address='0.0.0.0', port=9443).close_future().sync()
```

As TCP client:

```python
from py_netty import Bootstrap, ChannelHandlerAdapter


class HttpHandler(ChannelHandlerAdapter):
    def channel_read(self, ctx, buffer):
        print(buffer.decode('utf-8'))
        

remote_address, remote_port = 'www.google.com', 80
b = Bootstrap(handler_initializer=HttpHandler)
channel = b.connect(remote_address, remote_port).sync().channel()
request = f'GET / HTTP/1.1\r\nHost: {remote_address}\r\n\r\n'
channel.write(request.encode('utf-8'))
input() # pause
channel.close()
```


As TCP client (TLS):

```python
from py_netty import Bootstrap, ChannelHandlerAdapter


class HttpHandler(ChannelHandlerAdapter):
    def channel_read(self, ctx, buffer):
        print(buffer.decode('utf-8'))
        

remote_address, remote_port = 'www.google.com', 443
b = Bootstrap(handler_initializer=HttpHandler, tls=True, verify=True)
channel = b.connect(remote_address, remote_port).sync().channel()
request = f'GET / HTTP/1.1\r\nHost: {remote_address}\r\n\r\n'
channel.write(request.encode('utf-8'))
input() # pause
channel.close()
```

TCP port forwarding:

```python
from py_netty import ServerBootstrap, Bootstrap, ChannelHandlerAdapter, EventLoopGroup


class ProxyChannelHandler(ChannelHandlerAdapter):

    def __init__(self, remote_host, remote_port, client_eventloop_group):
        self._remote_host = remote_host
        self._remote_port = remote_port
        self._client_eventloop_group = client_eventloop_group
        self._client = None

    def _client_channel(self, ctx0):

        class __ChannelHandler(ChannelHandlerAdapter):
            def channel_read(self, ctx, bytebuf):
                ctx0.write(bytebuf)

            def channel_inactive(self, ctx):
                ctx0.close()

        if self._client is None:
            self._client = Bootstrap(
                eventloop_group=self._client_eventloop_group,
                handler_initializer=__ChannelHandler
            ).connect(self._remote_host, self._remote_port).sync().channel()
        return self._client

    def exception_caught(self, ctx, exception):
        ctx.close()

    def channel_read(self, ctx, bytebuf):
        self._client_channel(ctx).write(bytebuf)

    def channel_inactive(self, ctx):
        if self._client:
            self._client.close()


proxied_server, proxied_port = 'www.google.com', 443
client_eventloop_group = EventLoopGroup(1, 'ClientEventloopGroup')
sb = ServerBootstrap(
    parent_group=EventLoopGroup(1, 'Acceptor'),
    child_group=EventLoopGroup(1, 'Worker'),
    child_handler_initializer=lambda: ProxyChannelHandler(proxied_server, proxied_port, client_eventloop_group)
)
sb.bind(port=8443).close_future().sync()
```

## Event-driven callbacks

Create handler with callbacks for interested events:

``` python
from py_netty import ChannelHandlerAdapter


class MyChannelHandler(ChannelHandlerAdapter):
    def channel_active(self, ctx: 'ChannelHandlerContext') -> None:
        # invoked when channel is active (TCP connection ready)
        pass

    def channel_read(self, ctx: 'ChannelHandlerContext', msg: Union[bytes, socket.socket]) -> None:
        # invoked when there is data ready to process
        pass

    def channel_inactive(self, ctx: 'ChannelHandlerContext') -> None:
        # invoked when channel is inactive (TCP connection is broken)
        pass

    def channel_registered(self, ctx: 'ChannelHandlerContext') -> None:
        # invoked when the channel is registered with a eventloop
        pass

    def channel_unregistered(self, ctx: 'ChannelHandlerContext') -> None:
        # invoked when the channel is unregistered from a eventloop
        pass

    def channel_handshake_complete(self, ctx: 'ChannelHandlerContext') -> None:
        # invoked when ssl handshake is complete, this only applies to client side
        pass

    def channel_writability_changed(self, ctx: 'ChannelHandlerContext') -> None:
        # invoked when pending data > high water mark or < low water mark
        pass

    def exception_caught(self, ctx: 'ChannelHandlerContext', exception: Exception) -> None:
        # invoked when there is any exception raised during process
        pass
```


## Benchmark

The current benchmark uses the local echo performance runner in
`integration_tests/perf_echo.py`. Each case starts an in-process localhost echo
server for the selected engine, sends framed payloads from matching clients,
validates every echo, and reports throughput, message rate, latency, and
connection ramp-up time.

The following results were collected locally with:

```bash
python integration_tests/perf_echo.py --case all --engine all --timeout 20 --json
```

Environment: macOS 26.5 arm64, Python 3.12.10.

| Case | Engine | Connections | Payload | Messages | Throughput | Message rate | p50 latency | p95 latency | Ramp-up |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `single_connection_latency` | `py-netty` | 1 | 64 B | 200 | 0.45 MiB/s | 7,359 msg/s | 0.12 ms | 0.20 ms | 0.46 ms |
| `single_connection_latency` | `asyncio` | 1 | 64 B | 200 | 0.70 MiB/s | 11,516 msg/s | 0.07 ms | 0.13 ms | 0.35 ms |
| `single_connection_latency` | `threaded` | 1 | 64 B | 200 | 1.43 MiB/s | 23,511 msg/s | 0.04 ms | 0.05 ms | 1.38 ms |
| `backpressure_smoke` | `py-netty` | 8 | 64 KiB | 256 | 306.21 MiB/s | 4,899 msg/s | 42.11 ms | 47.05 ms | 6.89 ms |
| `backpressure_smoke` | `asyncio` | 8 | 64 KiB | 256 | 817.40 MiB/s | 13,078 msg/s | 14.08 ms | 18.48 ms | 0.84 ms |
| `backpressure_smoke` | `threaded` | 8 | 64 KiB | 256 | 812.25 MiB/s | 12,996 msg/s | 9.83 ms | 13.02 ms | 0.80 ms |
| `large_payload_throughput` | `py-netty` | 16 | 64 KiB | 512 | 620.71 MiB/s | 9,931 msg/s | 37.45 ms | 47.49 ms | 3.01 ms |
| `large_payload_throughput` | `asyncio` | 16 | 64 KiB | 512 | 751.79 MiB/s | 12,029 msg/s | 31.17 ms | 39.86 ms | 1.45 ms |
| `large_payload_throughput` | `threaded` | 16 | 64 KiB | 512 | 785.02 MiB/s | 12,560 msg/s | 19.12 ms | 27.11 ms | 1.64 ms |
| `small_payload_concurrency` | `py-netty` | 32 | 1 KiB | 6,400 | 49.05 MiB/s | 50,230 msg/s | 110.78 ms | 117.80 ms | 14.97 ms |
| `small_payload_concurrency` | `asyncio` | 32 | 1 KiB | 6,400 | 77.80 MiB/s | 79,665 msg/s | 43.25 ms | 69.66 ms | 2.78 ms |
| `small_payload_concurrency` | `threaded` | 32 | 1 KiB | 6,400 | 35.56 MiB/s | 36,416 msg/s | 88.69 ms | 117.45 ms | 2.39 ms |
| `connection_ramp_up` | `py-netty` | 64 | 64 B | 64 | 0.93 MiB/s | 15,272 msg/s | 3.18 ms | 3.95 ms | 12.93 ms |
| `connection_ramp_up` | `asyncio` | 64 | 64 B | 64 | 1.28 MiB/s | 20,967 msg/s | 1.44 ms | 1.60 ms | 5.02 ms |
| `connection_ramp_up` | `threaded` | 64 | 64 B | 64 | 0.59 MiB/s | 9,691 msg/s | 2.71 ms | 5.48 ms | 4.18 ms |

Metrics are informational and environment-dependent. The comparison uses three
local implementations: `py-netty`, Python `asyncio`, and blocking sockets with
one thread per connection (`threaded`). The performance runner fails only on
functional problems such as missing echoes, payload mismatches, connection
failures, or timeouts.

### Throughput

![echo throughput comparison](img/perf_echo_throughput.png)

### Message Rate

![echo message rate comparison](img/perf_echo_message_rate.png)

### Latency

![echo latency comparison](img/perf_echo_latency.png)

### Connection Ramp-up

![echo connection ramp-up comparison](img/perf_echo_ramp_up.png)

## Caveats

- No pipeline, supports only one handler FOR NOW
- No batteries-included codecs FOR NOW
- No pool or refcnt for bytes buffer, bytes objects are created and consumed at your disposal
