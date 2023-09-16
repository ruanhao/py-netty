import typing
import socket
import dataclasses
from .eventloop import EventLoopGroup
from .channel import ChannelFuture, ChannelContext
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
        sock.connect((address, port))  # blocking api
        eventloop = self.eventloop_group.get_eventloop()
        return eventloop.register(sock, is_server=False, handler_initializer=self.handler_initializer)


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

        class _ServerChannelHandlerInitializer(ChannelHandlerAdapter):
            def initialize_child(this, ctx: ChannelContext, client_socket: socket.socket):
                logger.debug("Initializing client socket: %s", client_socket)
                client_socket.setblocking(0)
                child_eventloop = self.child_group.get_eventloop()
                child_eventloop.register(client_socket, is_server=False, handler_initializer=self.child_handler_initializer)

        return eventloop.register(server_socket, is_server=True, handler_initializer=_ServerChannelHandlerInitializer)
