# py-netty :rocket:

An epoll-based event-driven TCP networking framework.

Ideas and concepts under the hood are build upon those of [Netty](https://netty.io/), especially the IO and executor model.

APIs are intuitive to use if you are a Netty alcoholic.




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
            ).connect(self._remote_host, self._remote_port).channel()
        return self._client

    def exception_caught(self, ctx, exception):
        ctx.close()

    def channel_read(self, ctx, bytebuf):
        self._client_channel(ctx).write(bytebuf)

    def channel_inactive(self, ctx):
        if self._client:
            self._client.close()


proxied_server, proxied_port = 'www.google.com', 443
client_eventloop_group = EventLoopGroup(2, 'ClientEventloopGroup')
sb = ServerBootstrap(
    parant_group=EventLoopGroup(1, 'Acceptor'),
    child_group=EventLoopGroup(2, 'Worker'),
    child_handler_initializer=lambda: ProxyChannelHandler(proxied_server, proxied_port, client_eventloop_group)
)
sb.bind(port=8443).close_future().sync()
```


## Performance Test

![RTT with small packet](https://raw.githubusercontent.com/ruanhao/py-netty/master/rtts_512_32.png)

![RTT with large packet](https://raw.githubusercontent.com/ruanhao/py-netty/master/rtts_512_2048.png)


## Caveats

- No pipeline, supports only one handler FOR NOW
- No batteries-included codecs FOR NOW
- No pool or refcnt for bytes buffer, bytes objects are created and consumed at your disposal


