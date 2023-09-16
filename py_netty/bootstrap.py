import typing
import socket
import dataclasses
from .eventloop import EventLoopGroup
from .channel import ChannelFuture, ChannelContext
from .handler import NoOpChannelHandler, EchoChannelHandler, AbstractChannelHandler
import logging


logger = logging.getLogger(__name__)


@dataclasses.dataclass(kw_only=True)
class Bootstrap:
    eventloop_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    handler: typing.Type[AbstractChannelHandler] = dataclasses.field(default_factory=NoOpChannelHandler)

    def connect(self, address, port) -> ChannelFuture:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((address, port))  # blocking api
        eventloop = self.eventloop_group.get_eventloop()
        return eventloop.register(sock, is_server=False, handler=self.handler)


@dataclasses.dataclass(kw_only=True)
class ServerBootstrap:
    parant_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    child_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    child_handler: typing.Type[AbstractChannelHandler] = dataclasses.field(default_factory=EchoChannelHandler)

    def bind(self, address='localhost', port=-1) -> ChannelFuture:
        assert port > 0
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((address, port))
        server_socket.listen(128)
        server_socket.setblocking(0)
        eventloop = self.parant_group.get_eventloop()

        class _ServerChannelHandler(NoOpChannelHandler):
            def channel_read(this, ctx: ChannelContext, client_socket: socket.socket):
                logger.debug("Initializing client socket: %s", client_socket)
                client_socket.setblocking(0)
                child_eventloop = self.child_group.get_eventloop()
                child_eventloop.register(client_socket, is_server=False, handler=self.child_handler)

        return eventloop.register(server_socket, is_server=True, handler=_ServerChannelHandler())
