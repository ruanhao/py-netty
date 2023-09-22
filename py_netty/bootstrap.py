import ssl
import typing
import socket
import logging
from .utils import recvall
import dataclasses
from functools import lru_cache
from .eventloop import EventLoopGroup
from .handler import EchoChannelHandler, ChannelHandlerAdapter
from .channel import ChannelFuture, ChannelContext, NioSocketChannel, NioServerSocketChannel


logger = logging.getLogger(__name__)


def _handler_initializer():
    return EchoChannelHandler()


@lru_cache(maxsize=8)
def _client_ssl_context(verify=True):
    if verify:
        return ssl.create_default_context()
    else: # no verify
        return ssl._create_unverified_context()


@lru_cache(maxsize=8)
def _server_ssl_context(certfile, keyfile):
    s_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    s_context.load_cert_chain(certfile, keyfile)
    return s_context


@dataclasses.dataclass
class Bootstrap:
    eventloop_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    handler_initializer: typing.Callable = _handler_initializer
    tls: bool = False
    verify: bool = True

    def connect(self, address, port, ensure_connected: bool = False) -> ChannelFuture:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # if ensure_connected or self.tls:
        if ensure_connected:
            sock.connect((address, port))
            if self.tls:
                sock = _client_ssl_context(self.verify).wrap_socket(sock, server_hostname=address)
            sock.setblocking(False)
        else:
            sock.setblocking(False)
            if self.tls:
                sock = _client_ssl_context(self.verify).wrap_socket(sock, server_hostname=address)
            sock.connect_ex((address, port))  # non blocking
        return NioSocketChannel(
            self.eventloop_group.get_eventloop(),
            sock,
            handler_initializer=self.handler_initializer
        ).register()


@dataclasses.dataclass
class ServerBootstrap:
    parant_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    child_group: EventLoopGroup = dataclasses.field(default_factory=EventLoopGroup)
    child_handler_initializer: typing.Callable = _handler_initializer
    certfile: str = None
    keyfile: str = None

    def bind(self, address='localhost', port=-1) -> ChannelFuture:
        assert port > 0
        assert ((self.certfile is not None) ^ (self.keyfile is not None)) is False, "Both certfile and keyfile must be specified"
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.certfile and self.keyfile:
            server_socket = _server_ssl_context(self.certfile, self.keyfile).wrap_socket(server_socket, server_side=True)
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
