import logging

import pytest

from py_netty import bootstrap as bootstrap_module
from py_netty.bootstrap import (
    Bootstrap,
    ServerBootstrap,
    _client_ssl_context,
    _handler_initializer,
    _server_ssl_context,
)
from py_netty.handler import EchoChannelHandler


class FakeSocket:

    def __init__(self, label="socket"):
        self.label = label
        self.connected = []
        self.connect_ex_calls = []
        self.blocking_values = []
        self.sockopts = []
        self.bound = []
        self.listen_backlogs = []

    def connect(self, address):
        self.connected.append(address)

    def connect_ex(self, address):
        self.connect_ex_calls.append(address)
        return 0

    def setblocking(self, value):
        self.blocking_values.append(value)

    def setsockopt(self, *args):
        self.sockopts.append(args)

    def bind(self, address):
        self.bound.append(address)

    def listen(self, backlog):
        self.listen_backlogs.append(backlog)

    def fileno(self):
        return 100

    def __repr__(self):
        return f"FakeSocket({self.label})"


class FakeSslContext:

    def __init__(self, protocol=None):
        self.protocol = protocol
        self.loaded_cert_chain = []
        self.ciphers = []
        self.minimum_version = None
        self.wrapped = []
        self.next_socket = None

    def load_cert_chain(self, certfile, keyfile):
        self.loaded_cert_chain.append((certfile, keyfile))

    def set_ciphers(self, ciphers):
        self.ciphers.append(ciphers)

    def wrap_socket(self, sock, **kwargs):
        self.wrapped.append((sock, kwargs))
        if self.next_socket is None:
            self.next_socket = FakeSocket("ssl-wrapped")
        return self.next_socket


class FakeEventLoopGroup:

    def __init__(self, eventloop):
        self.eventloop = eventloop
        self.calls = 0

    def get_eventloop(self):
        self.calls += 1
        return self.eventloop


class FakeNioSocketChannel:

    instances = []

    def __init__(self, eventloop, sock, handler_initializer):
        self.eventloop = eventloop
        self.sock = sock
        self.handler_initializer = handler_initializer
        self.registered = False
        self.future = object()
        self.__class__.instances.append(self)

    def register(self):
        self.registered = True
        return self.future


class FakeNioServerSocketChannel:

    instances = []

    def __init__(self, eventloop, sock, handler_initializer):
        self.eventloop = eventloop
        self.sock = sock
        self.handler_initializer = handler_initializer
        self.registered = False
        self.future = object()
        self.__class__.instances.append(self)

    def register(self):
        self.registered = True
        return self.future


@pytest.fixture(autouse=True)
def clear_ssl_context_caches():
    _client_ssl_context.cache_clear()
    _server_ssl_context.cache_clear()
    FakeNioSocketChannel.instances.clear()
    FakeNioServerSocketChannel.instances.clear()
    yield
    _client_ssl_context.cache_clear()
    _server_ssl_context.cache_clear()


def test_handler_initializer_returns_echo_handler():
    assert isinstance(_handler_initializer(), EchoChannelHandler)


class TestSslContextHelpers:

    def test_client_ssl_context_verify_uses_default_context_and_cache(self, monkeypatch):
        contexts = []

        def create_default_context():
            ctx = FakeSslContext()
            contexts.append(ctx)
            return ctx

        monkeypatch.setattr(bootstrap_module.ssl, "create_default_context", create_default_context)

        first = _client_ssl_context(True)
        second = _client_ssl_context(True)

        assert first is second
        assert contexts == [first]

    def test_client_ssl_context_without_verify_uses_unverified_context(self, monkeypatch):
        ctx = FakeSslContext()
        monkeypatch.setattr(bootstrap_module.ssl, "_create_unverified_context", lambda: ctx)

        result = _client_ssl_context(False)

        assert result is ctx
        assert ctx.minimum_version == bootstrap_module.ssl.TLSVersion.TLSv1_2
        assert ctx.ciphers == ["ALL"]

    def test_server_ssl_context_loads_cert_chain_and_caches(self, monkeypatch):
        contexts = []

        def ssl_context(protocol):
            ctx = FakeSslContext(protocol)
            contexts.append(ctx)
            return ctx

        monkeypatch.setattr(bootstrap_module.ssl, "SSLContext", ssl_context)

        first = _server_ssl_context("cert.pem", "key.pem")
        second = _server_ssl_context("cert.pem", "key.pem")

        assert first is second
        assert contexts == [first]
        assert first.protocol == bootstrap_module.ssl.PROTOCOL_TLS_SERVER
        assert first.loaded_cert_chain == [("cert.pem", "key.pem")]


class TestBootstrapSslMethods:

    def test_create_ssl_context_runs_callback(self, monkeypatch):
        ctx = FakeSslContext()
        calls = []
        monkeypatch.setattr(bootstrap_module, "_client_ssl_context", lambda verify: ctx)
        bootstrap = Bootstrap(verify=False, ssl_context_cb=calls.append)

        assert bootstrap._create_ssl_context() is ctx
        assert calls == [ctx]

    def test_create_ssl_context_logs_callback_error(self, monkeypatch, caplog):
        ctx = FakeSslContext()
        monkeypatch.setattr(bootstrap_module, "_client_ssl_context", lambda verify: ctx)

        def callback(_ctx):
            raise RuntimeError("bad callback")

        bootstrap = Bootstrap(ssl_context_cb=callback)
        caplog.set_level(logging.ERROR, logger="py_netty.bootstrap")

        assert bootstrap._create_ssl_context() is ctx
        assert "Error in ssl_context_cb(client): bad callback" in caplog.text

    def test_wrap_ssl_socket_uses_sni_or_address(self, monkeypatch):
        ctx = FakeSslContext()
        monkeypatch.setattr(bootstrap_module, "_client_ssl_context", lambda verify: ctx)
        bootstrap = Bootstrap()
        sock = FakeSocket("plain")

        wrapped = bootstrap._wrap_ssl_socket(sock, "example.com", "sni.example.com")
        wrapped_by_address = bootstrap._wrap_ssl_socket(sock, "example.org")

        assert wrapped is ctx.wrapped[0][0] or wrapped is ctx.next_socket
        assert wrapped_by_address is ctx.next_socket
        assert ctx.wrapped[0] == (sock, {"server_hostname": "sni.example.com"})
        assert ctx.wrapped[1] == (sock, {"server_hostname": "example.org"})


class TestBootstrapConnect:

    def patch_connect_dependencies(self, monkeypatch, sock):
        group = FakeEventLoopGroup("client-loop")
        monkeypatch.setattr(bootstrap_module.socket, "socket", lambda *args: sock)
        monkeypatch.setattr(bootstrap_module, "NioSocketChannel", FakeNioSocketChannel)
        return group

    def test_connect_uses_nonblocking_connect_ex_by_default(self, monkeypatch):
        sock = FakeSocket("plain")
        group = self.patch_connect_dependencies(monkeypatch, sock)
        handler_initializer = object()
        bootstrap = Bootstrap(eventloop_group=group, handler_initializer=handler_initializer)

        future = bootstrap.connect("example.com", 80)

        channel = FakeNioSocketChannel.instances[-1]
        assert future is channel.future
        assert sock.blocking_values == [False]
        assert sock.connect_ex_calls == [("example.com", 80)]
        assert sock.connected == []
        assert channel.eventloop == "client-loop"
        assert channel.sock is sock
        assert channel.handler_initializer is handler_initializer
        assert channel.registered is True

    def test_connect_ensure_connected_uses_blocking_connect_then_nonblocking(self, monkeypatch):
        sock = FakeSocket("plain")
        group = self.patch_connect_dependencies(monkeypatch, sock)
        bootstrap = Bootstrap(eventloop_group=group)

        bootstrap.connect("example.com", 443, ensure_connected=True)

        assert sock.connected == [("example.com", 443)]
        assert sock.connect_ex_calls == []
        assert sock.blocking_values == [False]
        assert FakeNioSocketChannel.instances[-1].sock is sock

    def test_connect_ensure_connected_tls_wraps_with_sni(self, monkeypatch):
        sock = FakeSocket("plain")
        wrapped = FakeSocket("wrapped")
        group = self.patch_connect_dependencies(monkeypatch, sock)
        calls = []
        bootstrap = Bootstrap(eventloop_group=group, tls=True)

        def wrap_ssl_socket(self, sock_arg, address, sni=None):
            calls.append((sock_arg, address, sni))
            return wrapped

        monkeypatch.setattr(bootstrap_module.Bootstrap, "_wrap_ssl_socket", wrap_ssl_socket)

        bootstrap.connect("example.com", 443, ensure_connected=True, sni="custom.sni")

        assert calls == [(sock, "example.com", "custom.sni")]
        assert wrapped.blocking_values == [False]
        assert FakeNioSocketChannel.instances[-1].sock is wrapped

    def test_connect_nonblocking_tls_wraps_with_address_before_connect_ex(self, monkeypatch):
        sock = FakeSocket("plain")
        wrapped = FakeSocket("wrapped")
        group = self.patch_connect_dependencies(monkeypatch, sock)
        calls = []
        bootstrap = Bootstrap(eventloop_group=group, tls=True)

        def wrap_ssl_socket(self, sock_arg, address, sni=None):
            calls.append((sock_arg, address, sni))
            return wrapped

        monkeypatch.setattr(bootstrap_module.Bootstrap, "_wrap_ssl_socket", wrap_ssl_socket)

        bootstrap.connect("example.com", 443, sni="ignored.sni")

        assert sock.blocking_values == [False]
        assert calls == [(sock, "example.com", None)]
        assert wrapped.connect_ex_calls == [("example.com", 443)]
        assert FakeNioSocketChannel.instances[-1].sock is wrapped

    def test_connect_can_use_socksocket(self, monkeypatch):
        plain_sock = FakeSocket("plain")
        socks_sock = FakeSocket("socks")
        monkeypatch.setattr(bootstrap_module.socket, "socket", lambda *args: plain_sock)
        monkeypatch.setattr(bootstrap_module.socks, "socksocket", lambda *args: socks_sock)
        monkeypatch.setattr(bootstrap_module, "NioSocketChannel", FakeNioSocketChannel)
        bootstrap = Bootstrap(eventloop_group=FakeEventLoopGroup("client-loop"))

        bootstrap.connect("example.com", 80, use_socksocket=True)

        assert plain_sock.connect_ex_calls == []
        assert socks_sock.connect_ex_calls == [("example.com", 80)]
        assert FakeNioSocketChannel.instances[-1].sock is socks_sock


class TestServerBootstrapBind:

    def patch_bind_dependencies(self, monkeypatch, server_socket):
        parent_group = FakeEventLoopGroup("parent-loop")
        child_group = FakeEventLoopGroup("child-loop")
        monkeypatch.setattr(bootstrap_module.socket, "socket", lambda *args: server_socket)
        monkeypatch.setattr(bootstrap_module, "NioSocketChannel", FakeNioSocketChannel)
        monkeypatch.setattr(bootstrap_module, "NioServerSocketChannel", FakeNioServerSocketChannel)
        return parent_group, child_group

    def test_bind_rejects_invalid_port_and_partial_tls_config(self):
        with pytest.raises(AssertionError):
            ServerBootstrap().bind(port=0)
        with pytest.raises(AssertionError, match="Both certfile and keyfile"):
            ServerBootstrap(certfile="cert.pem").bind(port=8443)
        with pytest.raises(AssertionError, match="Both certfile and keyfile"):
            ServerBootstrap(keyfile="key.pem").bind(port=8443)

    def test_bind_configures_plain_server_socket_and_registers_server_channel(self, monkeypatch):
        server_socket = FakeSocket("server")
        parent_group, child_group = self.patch_bind_dependencies(monkeypatch, server_socket)
        bootstrap = ServerBootstrap(parant_group=parent_group, child_group=child_group)

        future = bootstrap.bind(address="127.0.0.1", port=8080)

        server_channel = FakeNioServerSocketChannel.instances[-1]
        assert future is server_channel.future
        assert server_socket.sockopts == [
            (bootstrap_module.socket.SOL_SOCKET, bootstrap_module.socket.SO_REUSEADDR, 1)
        ]
        assert server_socket.bound == [("127.0.0.1", 8080)]
        assert server_socket.listen_backlogs == [128]
        assert server_socket.blocking_values == [0]
        assert server_channel.eventloop == "parent-loop"
        assert server_channel.sock is server_socket
        assert server_channel.registered is True
        assert parent_group.calls == 1
        assert child_group.calls == 0

    def test_bind_tls_wraps_socket_and_logs_callback_error(self, monkeypatch, caplog):
        raw_socket = FakeSocket("server")
        wrapped_socket = FakeSocket("wrapped-server")
        ssl_ctx = FakeSslContext()
        ssl_ctx.next_socket = wrapped_socket
        parent_group, child_group = self.patch_bind_dependencies(monkeypatch, raw_socket)
        monkeypatch.setattr(bootstrap_module, "_server_ssl_context", lambda certfile, keyfile: ssl_ctx)

        def callback(_ctx):
            raise RuntimeError("bad server callback")

        bootstrap = ServerBootstrap(
            parant_group=parent_group,
            child_group=child_group,
            certfile="cert.pem",
            keyfile="key.pem",
            ssl_context_cb=callback,
        )
        caplog.set_level(logging.ERROR, logger="py_netty.bootstrap")

        bootstrap.bind(address="0.0.0.0", port=8443)

        assert "Error in ssl_context_cb(server): bad server callback" in caplog.text
        assert ssl_ctx.wrapped == [(raw_socket, {"server_side": True})]
        assert wrapped_socket.sockopts == [
            (bootstrap_module.socket.SOL_SOCKET, bootstrap_module.socket.SO_REUSEADDR, 1)
        ]
        assert wrapped_socket.bound == [("0.0.0.0", 8443)]
        assert FakeNioServerSocketChannel.instances[-1].sock is wrapped_socket

    def test_bind_tls_runs_successful_callback(self, monkeypatch):
        raw_socket = FakeSocket("server")
        ssl_ctx = FakeSslContext()
        calls = []
        parent_group, child_group = self.patch_bind_dependencies(monkeypatch, raw_socket)
        monkeypatch.setattr(bootstrap_module, "_server_ssl_context", lambda certfile, keyfile: ssl_ctx)
        bootstrap = ServerBootstrap(
            parant_group=parent_group,
            child_group=child_group,
            certfile="cert.pem",
            keyfile="key.pem",
            ssl_context_cb=calls.append,
        )

        bootstrap.bind(port=8443)

        assert calls == [ssl_ctx]

    def test_bind_tls_without_callback_still_wraps_socket(self, monkeypatch):
        raw_socket = FakeSocket("server")
        wrapped_socket = FakeSocket("wrapped-server")
        ssl_ctx = FakeSslContext()
        ssl_ctx.next_socket = wrapped_socket
        parent_group, child_group = self.patch_bind_dependencies(monkeypatch, raw_socket)
        monkeypatch.setattr(bootstrap_module, "_server_ssl_context", lambda certfile, keyfile: ssl_ctx)
        bootstrap = ServerBootstrap(
            parant_group=parent_group,
            child_group=child_group,
            certfile="cert.pem",
            keyfile="key.pem",
        )

        bootstrap.bind(port=8443)

        assert ssl_ctx.wrapped == [(raw_socket, {"server_side": True})]
        assert FakeNioServerSocketChannel.instances[-1].sock is wrapped_socket

    def test_server_initializer_registers_accepted_client_socket(self, monkeypatch):
        server_socket = FakeSocket("server")
        client_socket = FakeSocket("client")
        parent_group, child_group = self.patch_bind_dependencies(monkeypatch, server_socket)
        child_handler_initializer = object()
        bootstrap = ServerBootstrap(
            parant_group=parent_group,
            child_group=child_group,
            child_handler_initializer=child_handler_initializer,
        )
        bootstrap.bind(port=8080)

        initializer_cls = FakeNioServerSocketChannel.instances[-1].handler_initializer
        initializer_cls().channel_read(None, client_socket)

        client_channel = FakeNioSocketChannel.instances[-1]
        assert client_socket.blocking_values == [0]
        assert client_channel.eventloop == "child-loop"
        assert client_channel.sock is client_socket
        assert client_channel.handler_initializer is child_handler_initializer
        assert client_channel.registered is True
        assert child_group.calls == 1
