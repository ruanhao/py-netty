import typing
import socket
import dataclasses
from .eventloop import EventLoopGroup
from .channel import ChannelFuture, ChannelContext, NioSocketChannel, NioServerSocketChannel
from .handler import EchoChannelHandler, ChannelHandlerAdapter
import logging


logger = logging.getLogger(__name__)


def _handler_initializer():
    return EchoChannelHandler()


@dataclasses.dataclass(kw_only=True)
class Bootstrap:
    eventloop_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    handler_initializer: typing.Callable = _handler_initializer

    def connect(self, address, port) -> ChannelFuture:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.connect_ex((address, port))  # non blocking
        return NioSocketChannel(
            self.eventloop_group.get_eventloop(),
            sock,
            handler_initializer=self.handler_initializer
        ).register()


@dataclasses.dataclass(kw_only=True)
class ServerBootstrap:
    parant_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    child_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    child_handler_initializer: typing.Callable = _handler_initializer

    def bind(self, address='localhost', port=-1) -> ChannelFuture:
        assert port > 0
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((address, port))
        server_socket.listen(128)
        server_socket.setblocking(0)
        eventloop = self.parant_group.get_eventloop()

        class _ChannelInitializer(ChannelHandlerAdapter):
            def channel_read(this, ctx: ChannelContext, client_socket: socket.socket):
                logger.debug("Initializing client socket: %s", client_socket)
                client_socket.setblocking(0)
                NioSocketChannel(
                    self.child_group.get_eventloop(),
                    client_socket,
                    handler_initializer=self.child_handler_initializer
                ).register()

        return NioServerSocketChannel(eventloop, server_socket, handler_initializer=_ChannelInitializer).register()
        # return eventloop.register(server_socket, is_server=True, handler_initializer=_ChannelInitializer)
