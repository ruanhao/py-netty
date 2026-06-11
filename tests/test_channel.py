import errno
import selectors
import socket

import pytest

from py_netty import channel as channel_module
from py_netty.bytebuf import Chunk, EMPTY_BUFFER
from py_netty.channel import (
    ChannelContext,
    ChannelFuture,
    ChannelHandlerContext,
    ChannelInfo,
    NioServerSocketChannel,
    NioSocketChannel,
    adaptive_bufsize,
)


class FakeEventLoop:

    def __init__(self, in_loop=True, modify_error=None):
        self.in_loop = in_loop
        self.modify_error = modify_error
        self.tasks = []
        self.modified = []
        self.registered = []
        self.unregistered = []
        self.closed = []

    def in_eventloop(self):
        return self.in_loop

    def submit_task(self, task):
        self.tasks.append(task)

    def modify_flag(self, channel):
        if self.modify_error:
            raise self.modify_error
        self.modified.append((channel, channel.flag()))

    def register(self, channel):
        self.registered.append(channel)
        return channel.channel_future()

    def unregister(self, channel):
        self.unregistered.append(channel)
        return channel.channel_future()

    def _close_channel_internally(self, channel, reason=""):
        self.closed.append((channel, reason))
        channel.close_future().set(channel)


class FakeSocket:

    def __init__(self, fileno=42, recv_chunks=None, send_results=None, peek_result=b""):
        self._fileno = fileno
        self.recv_chunks = list(recv_chunks or [])
        self.send_results = list(send_results or [])
        self.peek_result = peek_result
        self.sent = []
        self.closed = False
        self.setsockopt_calls = []

    def fileno(self):
        return -1 if self.closed else self._fileno

    def setsockopt(self, *args):
        self.setsockopt_calls.append(args)

    def send(self, data):
        self.sent.append(data)
        result = self.send_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def recv(self, bufsize, flags=0):
        if flags:
            if isinstance(self.peek_result, Exception):
                raise self.peek_result
            return self.peek_result
        result = self.recv_chunks.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True

    def getsockname(self):
        return ("127.0.0.1", 10000)

    def getpeername(self):
        return ("127.0.0.1", 10001)


class NoTcpNoDelaySocket(FakeSocket):

    def setsockopt(self, *args):
        raise OSError("unsupported option")


class NoPeerSocket(FakeSocket):

    def getpeername(self):
        raise OSError("not connected")


class BrokenInfoSocket(NoPeerSocket):

    def getsockname(self):
        raise OSError("closed")

    def __str__(self):
        return "socket-left - socket-right"


class FakeServerSocket(FakeSocket):

    def __init__(self, accepted):
        super().__init__()
        self.accepted = list(accepted)

    def accept(self):
        if not self.accepted:
            raise socket.error()
        return self.accepted.pop(0)


class RecordingHandler:

    def __init__(self):
        self.events = []
        self.exceptions = []

    def channel_active(self, ctx):
        self.events.append(("active", ctx))

    def channel_read(self, ctx, msg):
        self.events.append(("read", msg))

    def channel_inactive(self, ctx):
        self.events.append(("inactive", ctx))

    def channel_registered(self, ctx):
        self.events.append(("registered", ctx))

    def channel_unregistered(self, ctx):
        self.events.append(("unregistered", ctx))

    def channel_handshake_complete(self, ctx):
        self.events.append(("handshake_complete", ctx))

    def channel_writability_changed(self, ctx):
        self.events.append(("writability_changed", ctx))

    def exception_caught(self, ctx, exception):
        self.exceptions.append(exception)


class RaisingReadHandler(RecordingHandler):

    def channel_read(self, ctx, msg):
        raise RuntimeError("read failed")


class RaisingExceptionHandler(RecordingHandler):

    def exception_caught(self, ctx, exception):
        raise RuntimeError("exception handler failed")


class StubChannel:

    def __init__(self, handler=None):
        self.closed = False
        self.writes = []
        self._handler = handler or RecordingHandler()

    def close(self):
        self.closed = True

    def write(self, buffer):
        self.writes.append(buffer)
        return "write-result"

    def handler(self):
        return self._handler


def make_channel(eventloop=None, sock=None, handler_initializer=RecordingHandler):
    return NioSocketChannel(
        eventloop or FakeEventLoop(),
        sock or FakeSocket(),
        handler_initializer=handler_initializer,
    )


class TestChannelInfo:

    def test_of_captures_socket_identity_and_addresses(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.settimeout(1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(listener.getsockname())

                conn, _ = listener.accept()
                with conn:
                    info = ChannelInfo.of(client)

                    assert info.sock is client
                    assert info.id == hex(id(client))
                    assert info.sockname == client.getsockname()[:2]
                    assert info.peername == client.getpeername()[:2]
                    assert info.fileno == client.fileno()


class TestAdaptiveBufsize:

    def test_shrinks_without_going_below_minimum(self, monkeypatch):
        monkeypatch.setattr(channel_module, "_MIN_BUFFER_SIZE", 8)
        monkeypatch.setattr(channel_module, "_MAX_BUFFER_SIZE", 128)

        assert adaptive_bufsize(32, 8) == 16
        assert adaptive_bufsize(16, 1) == 8

    def test_grows_without_exceeding_maximum(self, monkeypatch):
        monkeypatch.setattr(channel_module, "_MIN_BUFFER_SIZE", 8)
        monkeypatch.setattr(channel_module, "_MAX_BUFFER_SIZE", 64)

        assert adaptive_bufsize(16, 16) == 32
        assert adaptive_bufsize(64, 64) == 64

    def test_keeps_size_when_thresholds_are_not_crossed(self, monkeypatch):
        monkeypatch.setattr(channel_module, "_MIN_BUFFER_SIZE", 8)
        monkeypatch.setattr(channel_module, "_MAX_BUFFER_SIZE", 64)

        assert adaptive_bufsize(32, 20) == 32


class TestChannelFuture:

    def test_set_done_sync_and_channel(self):
        channel = StubChannel()
        future = ChannelFuture(channel)

        assert future.channel() is channel
        assert future.done() is False

        future.set(channel)
        future.set(StubChannel())

        assert future.done() is True
        assert future.sync() is future
        assert future.future.result() is channel

    def test_add_listener_receives_channel_future(self):
        channel = StubChannel()
        future = ChannelFuture(channel)
        calls = []

        future.add_listener(calls.append)
        future.set(channel)

        assert calls == [future]

    def test_set_exception_makes_sync_raise(self):
        channel = StubChannel()
        future = ChannelFuture(channel)
        exception = OSError("connect failed")

        future.set_exception(exception)
        future.set_exception(OSError("ignored"))
        future.set(channel)

        assert future.done() is True
        with pytest.raises(OSError, match="connect failed"):
            future.sync()

    def test_close_future_delegates_to_channel_close_future(self):
        channel = make_channel()
        future = ChannelFuture(channel)

        assert future.close_future() is channel.close_future()


class TestAbstractChannelFlags:

    def test_basic_accessors_delegate_to_eventloop_and_socket(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)

        assert channel.channel_future().channel() is channel
        assert channel.eventloop() is eventloop
        assert channel.context().channel() is channel
        assert channel.socket() is channel._socket
        assert channel.fileno() == channel._socket.fileno()
        assert channel.fileno0() == 42

        assert channel.register() is channel.channel_future()
        assert channel.unregister() is channel.channel_future()
        assert eventloop.registered == [channel]
        assert eventloop.unregistered == [channel]

    def test_add_flag_submits_task_when_not_in_eventloop(self):
        eventloop = FakeEventLoop(in_loop=False)
        channel = make_channel(eventloop)

        channel.add_flag(selectors.EVENT_READ)

        assert channel.flag() == 0
        assert eventloop.modified == []
        assert len(eventloop.tasks) == 1

        eventloop.in_loop = True
        eventloop.tasks[0]()

        assert channel.flag() == selectors.EVENT_READ
        assert eventloop.modified == [(channel, selectors.EVENT_READ)]

    def test_add_flag_ignores_flag_that_is_already_set(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)
        channel.set_flag(selectors.EVENT_READ)

        channel.add_flag(selectors.EVENT_READ)

        assert channel.flag() == selectors.EVENT_READ
        assert eventloop.modified == []

    def test_add_flag_keeps_flag_when_modify_flag_raises(self, caplog):
        eventloop = FakeEventLoop(modify_error=RuntimeError("selector closed"))
        channel = make_channel(eventloop)

        channel.add_flag(selectors.EVENT_WRITE)

        assert channel.flag() == selectors.EVENT_WRITE
        assert eventloop.modified == []
        assert "selector closed" in caplog.text

    def test_add_and_remove_flag_log_debug_when_enabled(self, caplog):
        caplog.set_level("DEBUG", logger="py_netty.channel")
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)

        channel.add_flag(selectors.EVENT_READ)
        channel.remove_flag(selectors.EVENT_READ)

        assert "add flag" in caplog.text
        assert "remove flag" in caplog.text

    def test_remove_flag_submits_task_when_not_in_eventloop(self):
        eventloop = FakeEventLoop(in_loop=False)
        channel = make_channel(eventloop)
        channel.set_flag(selectors.EVENT_READ | selectors.EVENT_WRITE)

        channel.remove_flag(selectors.EVENT_READ)

        assert channel.flag() == selectors.EVENT_READ | selectors.EVENT_WRITE
        assert eventloop.modified == []
        assert len(eventloop.tasks) == 1

        eventloop.in_loop = True
        eventloop.tasks[0]()

        assert channel.flag() == selectors.EVENT_WRITE
        assert eventloop.modified == [(channel, selectors.EVENT_WRITE)]

    def test_remove_flag_ignores_flag_that_is_not_set(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)
        channel.set_flag(selectors.EVENT_WRITE)

        channel.remove_flag(selectors.EVENT_READ)

        assert channel.flag() == selectors.EVENT_WRITE
        assert eventloop.modified == []

    def test_remove_flag_clears_flag_and_modifies_eventloop(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)
        channel.set_flag(selectors.EVENT_READ | selectors.EVENT_WRITE)

        channel.remove_flag(selectors.EVENT_WRITE)

        assert channel.flag() == selectors.EVENT_READ
        assert eventloop.modified == [(channel, selectors.EVENT_READ)]

    def test_remove_flag_keeps_cleared_flag_when_modify_flag_raises(self, caplog):
        eventloop = FakeEventLoop(modify_error=RuntimeError("selector closed"))
        channel = make_channel(eventloop)
        channel.set_flag(selectors.EVENT_READ | selectors.EVENT_WRITE)

        channel.remove_flag(selectors.EVENT_READ)

        assert channel.flag() == selectors.EVENT_WRITE
        assert eventloop.modified == []
        assert "selector closed" in caplog.text


class TestAbstractChannelSetActive:

    def test_set_active_true_marks_channel_active_and_fires_active_event(self):
        """Test objective: switching from inactive to active marks prior activation and fires channel_active."""
        channel = make_channel()
        handler = channel.handler()

        channel.set_active(True, "connected")

        assert channel.is_active() is True
        assert channel._ever_active is True
        assert handler.events == [("active", channel.handler_context())]
        assert channel.channelinfo() is not None

    def test_set_active_false_fires_inactive_event(self):
        """Test objective: switching from active to inactive fires channel_inactive."""
        channel = make_channel()
        handler = channel.handler()
        channel.set_active(True)
        handler.events.clear()

        channel.set_active(False, "closed")

        assert channel.is_active() is False
        assert handler.events == [("inactive", channel.handler_context())]

    def test_set_active_same_state_does_not_fire_duplicate_events(self):
        """Test objective: setting the same active state again does not fire duplicate handler events."""
        channel = make_channel()
        handler = channel.handler()
        channel.set_active(True)
        handler.events.clear()

        channel.set_active(True)

        assert channel.is_active() is True
        assert handler.events == []

    def test_is_active_is_false_after_close_future_completes(self):
        channel = make_channel()
        channel.set_active(True)

        channel.close_future().set(channel)

        assert channel.is_active() is False


class TestAbstractChannelClose:

    def test_close_dispatches_force_and_graceful_paths(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)

        channel.close(force=True)
        channel.close(force=False)

        assert eventloop.closed == [
            (channel, "close channel forcibly"),
        ]

    def test_close_forcibly_submits_task_outside_eventloop(self):
        eventloop = FakeEventLoop(in_loop=False)
        channel = make_channel(eventloop)

        future = channel.close_forcibly()

        assert future is channel.close_future()
        assert eventloop.closed == []
        assert len(eventloop.tasks) == 1

        eventloop.in_loop = True
        eventloop.tasks[0]()
        assert eventloop.closed == [(channel, "close channel forcibly")]

    def test_close_gracefully_submits_task_outside_eventloop(self):
        eventloop = FakeEventLoop(in_loop=False)
        channel = make_channel(eventloop)

        future = channel.close_gracefully()

        assert future is channel.close_future()
        assert channel.pendings() == []
        assert len(eventloop.tasks) == 1

    def test_close_gracefully_returns_close_future_when_inactive(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)

        future = channel.close_gracefully()

        assert future is channel.close_future()
        assert channel.pendings() == []
        assert eventloop.closed == []

    def test_close_gracefully_closes_active_server_channel(self):
        eventloop = FakeEventLoop()
        channel = NioServerSocketChannel(eventloop, FakeServerSocket([]), RecordingHandler)
        channel.set_active(True)

        future = channel.close_gracefully()

        assert future is channel.close_future()
        assert eventloop.closed == [(channel, "close server channel gracefully")]

    def test_close_gracefully_enqueues_close_chunk_for_active_client(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)
        channel.set_active(True)

        future = channel.close_gracefully()

        assert future is channel.close_future()
        assert len(channel.pendings()) == 1
        assert channel.pendings()[0].close is True
        assert channel.pendings()[0].future is channel.close_future().future


class TestNioSocketChannel:

    def test_initial_state(self):
        eventloop = FakeEventLoop()
        sock = FakeSocket()
        channel = make_channel(eventloop, sock)

        assert channel.is_active() is False
        assert channel.is_writable() is True
        assert channel.is_auto_read() is True
        assert channel.is_server() is False
        assert channel.has_pendings() is False
        assert channel.connect_timeout_millis() == 3000
        assert sock.setsockopt_calls

    def test_ignores_tcp_nodelay_configuration_errors(self):
        channel = make_channel(sock=NoTcpNoDelaySocket())

        assert channel.is_writable() is True

    def test_ssl_handshake_state_helpers(self):
        channel = make_channel()
        channel._ssl_handshake_required = True
        channel._ssl_handshake_complete = False

        assert channel.needs_ssl_handshake() is True

        channel.set_ssl_handshake_complete()

        assert channel.needs_ssl_handshake() is False

    def test_add_pending_ignores_none_and_empty_non_close_chunks(self):
        channel = make_channel()

        channel.add_pending(None)
        channel.add_pending(Chunk(b""))

        assert channel.pendings() == []
        assert channel.has_pendings() is False

    def test_add_pending_queues_data_and_enables_write_flag(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)
        chunk = Chunk(b"abc")

        channel.add_pending(chunk)

        assert channel.pendings() == [chunk]
        assert channel.has_pendings() is True
        assert channel._pending_bytes == 3
        assert channel.flag() & selectors.EVENT_WRITE
        assert eventloop.modified == [(channel, selectors.EVENT_WRITE)]

    def test_add_pending_keeps_empty_close_chunk(self):
        channel = make_channel()
        chunk = Chunk(EMPTY_BUFFER, close=True)

        channel.add_pending(chunk)

        assert channel.pendings() == [chunk]

    def test_write_adds_pending_when_called_in_eventloop(self):
        channel = make_channel()

        future = channel.write(b"payload")

        assert isinstance(future, ChannelFuture)
        assert len(channel.pendings()) == 1
        assert channel.pendings()[0].buffer == b"payload"
        assert channel.pendings()[0].future is future.future

    def test_write_submits_task_when_called_outside_eventloop(self):
        eventloop = FakeEventLoop(in_loop=False)
        channel = make_channel(eventloop)

        future = channel.write(b"payload")

        assert isinstance(future, ChannelFuture)
        assert channel.pendings() == []
        assert len(eventloop.tasks) == 1

        eventloop.in_loop = True
        eventloop.tasks[0]()
        assert channel.pendings()[0].buffer == b"payload"
        assert channel.pendings()[0].future is future.future

    def test_fail_pendings_sets_exception_and_resets_state(self):
        channel = make_channel()
        write_chunk = Chunk(b"payload")
        close_chunk = Chunk(EMPTY_BUFFER, channel.close_future().future, True)
        channel._pendings = [write_chunk, close_chunk]
        channel._pending_bytes = len(write_chunk.buffer)

        channel.fail_pendings(RuntimeError("closed"))

        with pytest.raises(RuntimeError, match="closed"):
            write_chunk.future.result()
        assert channel.close_future().done() is False
        assert channel.pendings() == []
        assert channel._pending_bytes == 0

    def test_set_pendings_replaces_pending_list(self):
        channel = make_channel()
        chunks = [Chunk(b"abc")]

        channel.set_pendings(chunks)

        assert channel.pendings() is chunks

    def test_set_auto_read_toggles_read_flag(self):
        eventloop = FakeEventLoop()
        channel = make_channel(eventloop)
        channel.set_flag(selectors.EVENT_READ)

        channel.set_auto_read(False)
        channel.set_auto_read(False)
        channel.set_auto_read(True)

        assert channel.is_auto_read() is True
        assert eventloop.modified == [
            (channel, 0),
            (channel, selectors.EVENT_READ),
        ]

    def test_check_writability_fires_when_crossing_watermarks(self, monkeypatch):
        monkeypatch.setattr(channel_module, "_DEFAULT_LOW_WATER_MARK", 2)
        monkeypatch.setattr(channel_module, "_DEFAULT_HIGH_WATER_MARK", 4)
        channel = make_channel()
        handler = channel.handler()

        channel._pending_bytes = 4
        channel._check_writability()
        channel._pending_bytes = 2
        channel._check_writability()

        assert channel.is_writable() is True
        assert handler.events == [
            ("writability_changed", channel.handler_context()),
            ("writability_changed", channel.handler_context()),
        ]

    def test_try_send_returns_unsent_bytes_after_partial_send_and_errors(self):
        error = OSError(errno.EAGAIN, "try again")
        sock = FakeSocket(send_results=[2, error, error])
        channel = make_channel(sock=sock)

        assert channel.try_send(b"abcdef") == b"cdef"
        assert sock.sent == [b"abcdef", b"cdef", b"cdef"]

    def test_try_send_logs_socket_error_in_debug(self, caplog):
        caplog.set_level("DEBUG", logger="py_netty.channel")
        error = OSError(errno.EAGAIN, "try again")
        sock = FakeSocket(send_results=[error])
        channel = make_channel(sock=sock)

        assert channel.try_send(b"abc", spin=0) == b"abc"
        assert "try_send socket.error" in caplog.text

    def test_try_send_empty_bytes_returns_empty_bytes(self):
        channel = make_channel()

        assert channel.try_send(b"") == b""

    def test_recvall_returns_buffer_and_closed_on_eof(self):
        sock = FakeSocket(recv_chunks=[b"he", b"llo", b""])
        channel = make_channel(sock=sock)

        assert channel.recvall() == (b"hello", True)

    def test_recvall_returns_current_buffer_when_socket_would_block(self):
        error = OSError(errno.EAGAIN, "try again")
        sock = FakeSocket(recv_chunks=[b"hello", error], peek_result=b"")
        channel = make_channel(sock=sock)

        assert channel.recvall() == (b"hello", False)

    def test_recvall_continues_after_readable_socket_error(self, caplog):
        caplog.set_level("DEBUG", logger="py_netty.channel")
        error = OSError(errno.EAGAIN, "try again")
        sock = FakeSocket(recv_chunks=[error, b""], peek_result=b"x")
        channel = make_channel(sock=sock)

        assert channel.recvall() == (b"", True)
        assert "recvall socket.error" in caplog.text

    def test_recvall_debug_logs_when_yielding_for_slow_read(self, monkeypatch, caplog):
        caplog.set_level("DEBUG", logger="py_netty.channel")
        sock = FakeSocket(recv_chunks=[b"hello"])
        channel = make_channel(sock=sock)
        times = iter([1.0, 1.02])
        monkeypatch.setattr(channel_module.time, "perf_counter", lambda: next(times))

        assert channel.recvall() == (b"hello", False)
        assert "yield from recvall" in caplog.text

    def test_is_readable_uses_peek_without_consuming_data(self, monkeypatch):
        monkeypatch.setattr(channel_module.socket, "MSG_DONTWAIT", 0, raising=False)

        assert make_channel(sock=FakeSocket(peek_result=b"x")).is_readable() is True
        assert make_channel(sock=FakeSocket(peek_result=b"")).is_readable() is False
        assert make_channel(sock=FakeSocket(peek_result=OSError("closed"))).is_readable() is False


class TestChannelStringRepresentation:

    def test_channelinfo_lazily_populates_original_socket_info(self):
        channel = make_channel()
        channel._channelinfo = None

        assert channel.channelinfo() is not None

    def test_str_uses_question_mark_before_first_activation(self):
        channel = make_channel()

        assert " ? " in str(channel)

    def test_str_uses_exclamation_mark_after_inactive(self):
        channel = make_channel()
        channel.set_active(True)
        channel.set_active(False)

        assert " ! " in str(channel)

    def test_str_falls_back_to_sockinfo_when_channelinfo_unavailable_before_active(self):
        channel = make_channel(sock=BrokenInfoSocket())

        assert "?" in str(channel)

    def test_str_falls_back_to_sockinfo_when_channelinfo_unavailable_after_inactive(self):
        channel = make_channel(sock=BrokenInfoSocket())
        channel._ever_active = True
        channel._active = False

        assert "!" in str(channel)

    def test_str_returns_sockinfo_when_channelinfo_unavailable_and_active(self):
        channel = make_channel(sock=BrokenInfoSocket())
        channel._ever_active = True
        channel._active = True

        assert str(channel) == channel._sockinfo


class TestChannelContext:

    def test_close_write_and_channel_delegate_to_channel(self):
        channel = StubChannel()
        ctx = ChannelContext(channel)

        assert ctx.channel() is channel
        assert ctx.write(b"payload") is None
        ctx.close()

        assert channel.writes == [b"payload"]
        assert channel.closed is True


class TestChannelHandlerContext:

    def test_write_channel_and_handler_delegate_to_channel(self):
        handler = RecordingHandler()
        channel = StubChannel(handler)
        ctx = ChannelHandlerContext(channel)

        assert ctx.channel() is channel
        assert ctx.handler() is handler
        assert ctx.write(b"payload") == "write-result"
        assert channel.writes == [b"payload"]

        ctx.close()
        assert channel.closed is True

    @pytest.mark.parametrize(
        "fire_method,expected_event,args",
        [
            ("fire_channel_registered", "registered", ()),
            ("fire_channel_unregistered", "unregistered", ()),
            ("fire_channel_read", "read", (b"payload",)),
            ("fire_channel_active", "active", ()),
            ("fire_channel_inactive", "inactive", ()),
            ("fire_channel_writability_changed", "writability_changed", ()),
            ("fire_channel_handshake_complete", "handshake_complete", ()),
        ],
    )
    def test_fire_methods_dispatch_to_handler(self, fire_method, expected_event, args):
        handler = RecordingHandler()
        ctx = ChannelHandlerContext(StubChannel(handler))

        getattr(ctx, fire_method)(*args)

        assert handler.events[0][0] == expected_event

    def test_fire_method_forwards_handler_exception_to_exception_caught(self):
        handler = RaisingReadHandler()
        ctx = ChannelHandlerContext(StubChannel(handler))

        ctx.fire_channel_read(b"payload")

        assert len(handler.exceptions) == 1
        assert str(handler.exceptions[0]) == "read failed"

    def test_fire_exception_caught_logs_when_exception_handler_raises(self, caplog):
        handler = RaisingExceptionHandler()
        ctx = ChannelHandlerContext(StubChannel(handler))

        ctx.fire_exception_caught(RuntimeError("original"))

        assert "Exception caught while handling exception" in caplog.text


class TestNioServerSocketChannel:

    def test_marks_channel_as_server(self):
        channel = NioServerSocketChannel(FakeEventLoop(), FakeServerSocket([]), RecordingHandler)

        assert channel.is_server() is True
        assert channel.needs_ssl_handshake() is False
        assert channel.set_ssl_handshake_complete() is None

    def test_acceptall_returns_accepted_connections_until_socket_error(self):
        accepted = [
            ("client-1", ("127.0.0.1", 10001)),
            ("client-2", ("127.0.0.1", 10002)),
        ]
        channel = NioServerSocketChannel(FakeEventLoop(), FakeServerSocket(accepted), RecordingHandler)

        assert channel.acceptall() == accepted
