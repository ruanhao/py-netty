# py-netty :rocket:

An event-driven TCP networking framework.

Ideas and concepts under the hood are build upon those of [Netty](https://netty.io/), especially the IO and executor model.

APIs are intuitive to use if you are a Netty alcoholic.


# Features

- callback based application invocation
- non blocking IO
- recv/write is performed only in IO thread
- adaptive read buffer
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
ServerBootstrap(certfile='/path/to/cert/file', keyfile='/path/to/cert/file').bind(address='0.0.0.0', port=9443).close_future().sync()
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
    parant_group=EventLoopGroup(1, 'Acceptor'),
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

    def exception_caught(self, ctx: 'ChannelHandlerContext', exception: Exception) -> None:
        # invoked when there is any exception raised during process
        pass
```


## Benchmark

Test is performed using echo client/server mechanism on a 1-Core 2.0GHz Intel(R) Xeon(R) Platinum 8452Y with 4GB memory, Ubuntu 22.04.
(Please see `bm_echo_server.py` for details.)

3 methods are tested: 
1. BIO (Traditional thread based blocking IO)
2. Asyncio (Python built-in async IO)
3. NIO (py-netty with 1 eventloop)

3 metrics are collected:
1. Throughput (of each connection) to indicate overall stability
2. Average throughput (of all connections) to indicate overall performance
3. Ramp up time (seconds consumed after all connections established) to indicate responsiveness

### Case 1: Concurrent 64 connections with 32K/s 
![Throughput](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/64_concurrent_32K_throuput.png)
![Average Speed](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/64_concurrent_32K_average.png)
![Ramp Up Time](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/64_concurrent_32K_rampup.png)

### Case 2: Concurrent 64 connections with 4M/s 
![Throughput](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/64_concurrent_4M_throuput.png)
![Average Speed](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/64_concurrent_4M_average.png)
![Ramp Up Time](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/64_concurrent_4M_rampup.png)

### Case 3: Concurrent 128 connections with 4M/s 
![Throughput](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/128_concurrent_4M_throuput.png)
![Average Speed](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/128_concurrent_4M_average.png)
![Ramp Up Time](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/128_concurrent_4M_rampup.png)

### Case 4: Concurrent 128 connections with 8M/s 
![Throughput](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/128_concurrent_8M_throuput.png)
![Average Speed](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/128_concurrent_8M_average.png)
![Ramp Up Time](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/128_concurrent_8M_rampup.png)


### Case 5: Concurrent 256 connections with 8M/s 
![Throughput](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/256_concurrent_8M_throuput.png)
![Average Speed](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/256_concurrent_8M_average.png)
![Ramp Up Time](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/256_concurrent_8M_rampup.png)

### CPU Usage
![32K/s](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/cpu_usage_32k.png)
![2M/s](https://raw.githubusercontent.com/ruanhao/py-netty/master/img/cpu_usage_2m.png)

## Caveats

- No pipeline, supports only one handler FOR NOW
- No batteries-included codecs FOR NOW
- No pool or refcnt for bytes buffer, bytes objects are created and consumed at your disposal


