import ssl
import socks
import typing
import socket
import logging
from functools import lru_cache
from .eventloop import EventLoopGroup
from .handler import EchoChannelHandler, ChannelHandlerAdapter
from .channel import ChannelFuture, ChannelContext, NioSocketChannel, NioServerSocketChannel
from attrs import define, field


logger = logging.getLogger(__name__)


def _handler_initializer():
    return EchoChannelHandler()


def _resolve_tcp_addresses(address, port, flags=0):
    return socket.getaddrinfo(
        address,
        port,
        socket.AF_UNSPEC,
        socket.SOCK_STREAM,
        0,
        flags,
    )


def _resolve_tcp_address(address, port, flags=0):
    return _resolve_tcp_addresses(address, port, flags)[0]


def _is_ipv6_address(address):
    try:
        socket.inet_pton(socket.AF_INET6, address)
        return True
    except (OSError, TypeError):
        return False


@lru_cache(maxsize=8)
def _client_ssl_context(verify=True):
    if verify:
        return ssl.create_default_context()
    else:                       # no verify
        ssl_context = ssl._create_unverified_context()
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.set_ciphers("ALL")
        return ssl_context


@lru_cache(maxsize=8)
def _server_ssl_context(certfile, keyfile):
    s_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    s_context.load_cert_chain(certfile, keyfile)
    return s_context


@define(slots=True)
class Bootstrap:
    eventloop_group: EventLoopGroup = field(factory=EventLoopGroup)
    handler_initializer: typing.Callable = field(default=_handler_initializer)
    tls: bool = False
    verify: bool = True
    ssl_context_cb: typing.Callable = None

    def _create_ssl_context(self):
        ctx = _client_ssl_context(self.verify)
        if self.ssl_context_cb:
            try:
                self.ssl_context_cb(ctx)
            except Exception as e:
                logger.error("Error in ssl_context_cb(client): %s", e)
        return ctx

    def _wrap_ssl_socket(self, sock, server_hostname_or_address, sni: str | None = None) -> ssl.SSLSocket:
        return self._create_ssl_context().wrap_socket(
            sock,
            server_hostname=sni or server_hostname_or_address,
            do_handshake_on_connect=False,
        )

    def connect(self, address, port, ensure_connected: bool = False, sni: str | None = None, use_socksocket: bool = False) -> ChannelFuture:
        # if use_socksocket is enabled, please make sure socks.set_default_proxy() is prepared beforehand
        socket_factory = socks.socksocket if use_socksocket else socket.socket
        if use_socksocket and not _is_ipv6_address(address):
            resolved_addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, port))]
        else:
            resolved_addresses = _resolve_tcp_addresses(address, port)
        last_error = None
        sock = None

        # if ensure_connected or self.tls:
        for family, socktype, proto, _canonname, sockaddr in resolved_addresses:
            sock = socket_factory(family, socktype, proto)
            try:
                if ensure_connected:
                    sock.connect(sockaddr)
                    if self.tls:
                        sock = self._wrap_ssl_socket(sock, address, sni)
                    sock.setblocking(False)
                else:
                    sock.setblocking(False)
                    if self.tls:
                        sock = self._wrap_ssl_socket(sock, address, sni)
                    sock.connect_ex(sockaddr)  # non blocking
                break
            except Exception as e:
                last_error = e
                try:
                    sock.close()
                except Exception:
                    pass
        else:
            raise last_error or OSError(f"Could not resolve TCP address: {address}:{port}")

        return NioSocketChannel(
            self.eventloop_group.get_eventloop(),
            sock,
            handler_initializer=self.handler_initializer,
            ssl_handshake=self.tls,
        ).register()


@define(slots=True)
class ServerBootstrap:
    parent_group: EventLoopGroup = field(factory=EventLoopGroup)
    child_group: EventLoopGroup = field(factory=EventLoopGroup)
    child_handler_initializer: typing.Callable = field(default=_handler_initializer)
    certfile: str = None
    keyfile: str = None
    ssl_context_cb: typing.Callable = None

    def bind(self, address='localhost', port=-1) -> ChannelFuture:
        assert port > 0
        assert ((self.certfile is not None) ^ (self.keyfile is not None)) is False, "Both certfile and keyfile must be specified"
        family, socktype, proto, _canonname, sockaddr = _resolve_tcp_address(address, port, socket.AI_PASSIVE)
        server_socket = socket.socket(family, socktype, proto)
        ssl_ctx = None
        if self.certfile and self.keyfile:
            ssl_ctx = _server_ssl_context(self.certfile, self.keyfile)
            if self.ssl_context_cb:
                try:
                    self.ssl_context_cb(ssl_ctx)
                except Exception as e:
                    logger.error("Error in ssl_context_cb(server): %s", e)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(sockaddr)
        server_socket.listen(128)
        server_socket.setblocking(0)
        eventloop = self.parent_group.get_eventloop()

        class _ChannelInitializer(ChannelHandlerAdapter):
            def channel_read(this, ctx: ChannelContext, client_socket: socket.socket):
                logger.debug("Initializing client socket: %s", client_socket)
                client_socket.setblocking(0)
                ssl_handshake = False
                if ssl_ctx:
                    client_socket = ssl_ctx.wrap_socket(
                        client_socket,
                        server_side=True,
                        do_handshake_on_connect=False,
                    )
                    ssl_handshake = True
                NioSocketChannel(
                    self.child_group.get_eventloop(),
                    client_socket,
                    handler_initializer=self.child_handler_initializer,
                    ssl_handshake=ssl_handshake,
                ).register()

        return NioServerSocketChannel(eventloop, server_socket, handler_initializer=_ChannelInitializer).register()
        # return eventloop.register(server_socket, is_server=True, handler_initializer=_ChannelInitializer)
