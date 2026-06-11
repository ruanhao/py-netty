from concurrent.futures import Future

from py_netty import Bootstrap, ChannelHandlerAdapter, EventLoopGroup
from py_netty.channel import NioSocketChannel

REMOTE_ADDR = '127.0.0.1'
REMOTE_PORT = 9998
REMOTE_TLS_PORT = 9997


class LoggerChannelHandler(ChannelHandlerAdapter):

    def __init__(self, response: Future):
        super().__init__()
        self._response = response

    def channel_read(self, ctx, msg: bytes) -> None:
        print("<-", msg)
        self._response.set_result(msg)


class TestEchoChannel:

    def test_tcp(self):
        response = Future()

        with EventLoopGroup(1, "integration-client") as eventloop_group:
            b = Bootstrap(
                eventloop_group=eventloop_group,
                handler_initializer=lambda: LoggerChannelHandler(response),
            )
            channel = b.connect(REMOTE_ADDR, REMOTE_PORT).sync().channel()
            assert isinstance(channel, NioSocketChannel)
            channel.write(b"hello world").sync()

            assert response.result(timeout=3) == b"hello world"
            channel.close()
            channel.close_future().sync()

    def test_tls(self):
        response = Future()

        with EventLoopGroup(1, "integration-client") as eventloop_group:
            b = Bootstrap(
                eventloop_group=eventloop_group,
                handler_initializer=lambda: LoggerChannelHandler(response),
                tls=True,
                verify=False,
            )
            channel = b.connect(REMOTE_ADDR, REMOTE_TLS_PORT).sync().channel()
            assert isinstance(channel, NioSocketChannel)
            channel.write(b"hello world").sync()

            assert response.result(timeout=3) == b"hello world"
            channel.close()
            channel.close_future().sync()
