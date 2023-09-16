# py-netty :rocket:

An epoll-based TCP networking library for Python 3.7+.

APIs are similar to the [Netty](https://netty.io/) framework.



## Installation

```bash
python -m pip install py-netty
```

## Getting Started

Start an echo server:

```python
from py_netty import ServerBootstrap
ServerBootstrap().bind(address='0.0.0.0', port=8080)
```

As TCP client:

```python
from py_netty import Bootstrap
from py_netty.handler import NoOpChannelHandler


class HttpHandler(NoOpChannelHandler):

    def channel_read(self, ctx, buffer):
        print(buffer.decode('utf-8'))

b = Bootstrap(handler=HttpHandler())
channel = b.connect(remote_address, remote_port).sync()
request = f'GET / HTTP/1.1\r\nHost: {remote_address}\r\n\r\n'
channel.write(request.encode('utf-8'))
```

## Performance Test

![Example](https://raw.githubusercontent.com/ruanhao/py-netty/master/rtts_512_32.png)

![Example](https://raw.githubusercontent.com/ruanhao/py-netty/master/rtts_512_2048.png)


