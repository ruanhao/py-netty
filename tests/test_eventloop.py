import errno
import logging
import selectors
import socket
import threading
from concurrent.futures import Future

import pytest

from py_netty import eventloop as eventloop_module
from py_netty.bytebuf import Chunk
from py_netty.channel import ChannelFuture
from py_netty.eventloop import EventLoop, EventLoopGroup


class FakeEventFD:

    def __init__(self, fileno=10):
        self._fileno = fileno
        self.writes = []
        self.reads = 0

    def fileno(self):
        return self._fileno

    def unsafe_write(self):
        self.writes.append(True)

    def unsafe_read(self):
        self.reads += 1


class FakeSelector:

    def __init__(self):
        self._fd_to_key = {}
        self.registered = []
        self.modified = []
        self.unregistered = []
        self.select_timeouts = []
        self.select_events = []
        self.closed = False
        self.unregister_error = None

    def _fd(self, fileobj):
        return fileobj if isinstance(fileobj, int) else fileobj.fileno()

    def register(self, fileobj, events):
        fd = self._fd(fileobj)
        key = selectors.SelectorKey(fileobj, fd, events, None)
        self._fd_to_key[fd] = key
        self.registered.append((fileobj, events))
        return key

    def unregister(self, fileobj):
        if self.unregister_error:
            raise self.unregister_error
        fd = self._fd(fileobj)
        self.unregistered.append(fd)
        return self._fd_to_key.pop(fd)

    def get_key(self, fileobj):
        fd = self._fd(fileobj)
        if fd not in self._fd_to_key:
            raise KeyError(fd)
        return self._fd_to_key[fd]

    def modify(self, fileobj, events):
        fd = self._fd(fileobj)
        key = self.get_key(fd)
        self._fd_to_key[fd] = selectors.SelectorKey(key.fileobj, fd, events, key.data)
        self.modified.append((fd, events))

    def select(self, timeout=None):
        self.select_timeouts.append(timeout)
        if self.select_events:
            return self.select_events.pop(0)
        return []

    def close(self):
        self.closed = True

    def get_map(self):
        return self._fd_to_key


class FakePool:

    def __init__(self, workers=1, prefix="fake-pool"):
        self._max_workers = workers
        self._thread_name_prefix = prefix
        self.submitted = []
        self.shutdown_called = False

    def submit(self, fn):
        self.submitted.append(fn)
        return Future()

    def shutdown(self):
        self.shutdown_called = True


class FakeSocket:

    def __init__(self, fileno=100, connect_error=0, connect_error_exception=None):
        self._fileno = fileno
        self.connect_error = connect_error
        self.connect_error_exception = connect_error_exception
        self.closed = False
        self.blocking_values = []

    def fileno(self):
        return -1 if self.closed else self._fileno

    def setblocking(self, value):
        self.blocking_values.append(value)

    def close(self):
        self.closed = True

    def getsockopt(self, level, optname):
        if self.connect_error_exception:
            raise self.connect_error_exception
        assert level == socket.SOL_SOCKET
        assert optname == socket.SO_ERROR
        return self.connect_error

    def getpeername(self):
        return ("127.0.0.1", 10001)


class FakeHandlerContext:

    def __init__(self, channel):
        self.channel = channel

    def fire_channel_registered(self):
        self.channel.events.append(("registered", None))

    def fire_channel_unregistered(self):
        self.channel.events.append(("unregistered", None))

    def fire_channel_read(self, msg):
        self.channel.events.append(("read", msg))


class FakeChannel:

    def __init__(self, fileno=100, server=False, sock=None):
        self._fileno = fileno
        self._flag = 0
        self._ever_active = False
        self._pending_bytes = 0
        self._server = server
        self._socket = sock or FakeSocket(fileno)
        self._channel_future = ChannelFuture(self)
        self._close_future = ChannelFuture(self)
        self.events = []
        self.removed_flags = []
        self.writability_checks = 0
        self.unregister_calls = 0
        self.try_send_results = []
        self.recv_results = []
        self.accept_results = []
        self._pendings = []
        self.connect_timeout = 3000

    def __str__(self):
        return f"FakeChannel({self._fileno})"

    def id(self):
        return f"channel-{self._fileno}"

    def socket(self):
        return self._socket

    def fileno(self):
        return self._socket.fileno()

    def fileno0(self):
        return self._fileno

    def set_flag(self, flag):
        self._flag = flag

    def channel_future(self):
        return self._channel_future

    def close_future(self):
        return self._close_future

    def handler_context(self):
        return FakeHandlerContext(self)

    def is_server(self):
        return self._server

    def is_active(self):
        return self._ever_active

    def set_active(self, active, reason=""):
        self._ever_active = active
        self.events.append(("active", active, reason))

    def connect_timeout_millis(self):
        return self.connect_timeout

    def has_pendings(self):
        return bool(self._pendings)

    def pendings(self):
        return self._pendings

    def set_pendings(self, pendings):
        self._pendings = pendings

    def try_send(self, buffer):
        if self.try_send_results:
            return self.try_send_results.pop(0)
        return b""

    def recvall(self):
        return self.recv_results.pop(0)

    def acceptall(self):
        return self.accept_results

    def remove_flag(self, flag):
        self.removed_flags.append(flag)
        self._flag &= ~flag

    def _check_writability(self):
        self.writability_checks += 1

    def unregister(self):
        self.unregister_calls += 1
        return self.channel_future()


@pytest.fixture
def loop(monkeypatch):
    eventfd = FakeEventFD()
    selector = FakeSelector()
    monkeypatch.setattr(eventloop_module, "eventfd", lambda: eventfd)
    monkeypatch.setattr(eventloop_module.selectors, "DefaultSelector", lambda: selector)
    eventloop = EventLoop(FakePool())
    eventloop._start_barrier.set()
    return eventloop


def enter_loop(eventloop):
    eventloop._thread = threading.current_thread()
    return eventloop


def selector_key(fileobj):
    return selectors.SelectorKey(fileobj, fileobj.fileno(), 0, None)


class TestEventLoopBasics:

    def test_initialization_registers_eventfd_for_read(self, loop):
        assert loop._selector.registered == [(loop._eventfd, selectors.EVENT_READ)]

    def test_in_eventloop_depends_on_current_thread(self, loop):
        assert loop.in_eventloop() is False

        enter_loop(loop)

        assert loop.in_eventloop() is True

    def test_interrupt_writes_eventfd_and_counts_in_debug(self, loop, caplog):
        enter_loop(loop)
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")

        loop.interrupt("wake")

        assert loop._eventfd.writes == [True]
        assert loop._eventfd_write_count == 1

    def test_stop_sets_flag_and_interrupts(self, loop):
        loop.stop()

        assert loop._stop_polling is True
        assert loop._eventfd.writes == [True]


class TestModifyFlag:

    def test_modify_flag_submits_task_outside_eventloop(self, loop):
        channel = FakeChannel()

        loop.modify_flag(channel)

        assert loop._taskq.qsize() == 1
        assert loop._eventfd.writes == [True]

    def test_modify_flag_unregisters_when_flag_is_zero(self, loop):
        enter_loop(loop)
        channel = FakeChannel()
        loop._selector.register(channel, selectors.EVENT_READ)

        loop.modify_flag(channel)

        assert loop._selector.unregistered[-1] == channel.fileno()

    def test_modify_flag_registers_missing_open_channel(self, loop):
        enter_loop(loop)
        channel = FakeChannel()
        channel._flag = selectors.EVENT_READ

        loop.modify_flag(channel)

        assert loop._selector.registered[-1] == (channel, selectors.EVENT_READ)

    def test_modify_flag_ignores_missing_closed_channel(self, loop):
        enter_loop(loop)
        sock = FakeSocket()
        sock.close()
        channel = FakeChannel(sock=sock)
        channel._flag = selectors.EVENT_READ

        loop.modify_flag(channel)

        assert loop._selector.registered == [(loop._eventfd, selectors.EVENT_READ)]

    def test_modify_flag_modifies_existing_key(self, loop):
        enter_loop(loop)
        channel = FakeChannel()
        loop._selector.register(channel, selectors.EVENT_READ)
        channel._flag = selectors.EVENT_WRITE

        loop.modify_flag(channel)

        assert loop._selector.modified == [(channel.fileno(), selectors.EVENT_WRITE)]


class TestRegisterAndUnregister:

    def test_register_submits_task_outside_eventloop(self, loop):
        channel = FakeChannel()

        future = loop.register(channel)

        assert future is channel.channel_future()
        assert loop._taskq.qsize() == 1

    def test_register_ignores_closed_socket(self, loop):
        enter_loop(loop)
        sock = FakeSocket()
        sock.close()
        channel = FakeChannel(sock=sock)

        future = loop.register(channel)

        assert future is channel.channel_future()
        assert loop._selector.registered == [(loop._eventfd, selectors.EVENT_READ)]

    def test_register_client_sets_flags_and_timeout(self, loop, monkeypatch):
        enter_loop(loop)
        monkeypatch.setattr(eventloop_module.time, "time", lambda: 10.0)
        channel = FakeChannel()

        loop.register(channel)

        assert channel._flag == selectors.EVENT_READ | selectors.EVENT_WRITE
        assert channel.socket().blocking_values == [False]
        assert channel.events == [("registered", None)]
        assert loop._channels[channel.fileno()] is channel
        assert loop._connect_timeout_due_millis[channel.fileno()] == 13000

    def test_register_server_skips_connect_timeout(self, loop):
        enter_loop(loop)
        channel = FakeChannel(server=True)

        loop.register(channel, only_write=True)

        assert channel._flag == selectors.EVENT_WRITE
        assert channel.fileno() in loop._channels
        assert loop._connect_timeout_due_millis == {}

    def test_unregister_submits_task_outside_eventloop(self, loop):
        channel = FakeChannel()

        future = loop.unregister(channel)

        assert future.channel() is channel
        assert loop._taskq.qsize() == 1

    def test_unregister_cleans_state_and_sets_future(self, loop):
        enter_loop(loop)
        channel = FakeChannel()
        loop._selector.register(channel, selectors.EVENT_READ)
        loop._channels[channel.fileno()] = channel
        loop._connect_timeout_due_millis[channel.fileno()] = 100

        future = loop.unregister(channel)

        assert future.done() is True
        assert future.future.result() is channel
        assert channel.events == [("unregistered", None)]
        assert channel.fileno() not in loop._channels
        assert channel.fileno() not in loop._connect_timeout_due_millis

    def test_unregister_logs_debug_when_selector_unregisters(self, loop, caplog):
        enter_loop(loop)
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        channel = FakeChannel()
        loop._selector.register(channel, selectors.EVENT_READ)

        loop.unregister(channel)

        assert "unregistered channel channel-100/100 from selector" in caplog.text

    def test_unregister_cleans_state_when_selector_raises(self, loop):
        enter_loop(loop)
        channel = FakeChannel()
        loop._selector.unregister_error = KeyError("missing")
        loop._channels[channel.fileno()] = channel
        loop._connect_timeout_due_millis[channel.fileno()] = 100

        future = loop.unregister(channel)

        assert future.done() is True
        assert channel.fileno() not in loop._channels
        assert channel.fileno() not in loop._connect_timeout_due_millis

    def test_register_logs_debug_when_enabled(self, loop, caplog):
        enter_loop(loop)
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        channel = FakeChannel()

        loop.register(channel)

        assert "registered channel(server:False)" in caplog.text


class TestTaskQueueAndHelpers:

    def test_submit_task_queues_task_and_interrupts(self, loop):
        def task():
            return None

        loop.submit_task(task)

        assert loop._taskq.get_nowait() is task
        assert loop._total_tasks_submitted == 1
        assert loop._eventfd.writes == [True]

    def test_process_task_queue_runs_successful_and_failing_tasks(self, loop):
        calls = []

        def ok():
            calls.append("ok")

        def fail():
            raise RuntimeError("boom")

        loop._taskq.put(ok)
        loop._taskq.put(fail)

        loop._process_task_queue()

        assert calls == ["ok"]
        assert loop._total_tasks_processed == 2

    def test_process_task_queue_logs_debug_for_task_lifecycle(self, loop, caplog):
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")

        def ok():
            return None

        loop._taskq.put(ok)

        loop._process_task_queue()

        assert "task to run:" in caplog.text
        assert "task finished" in caplog.text

    def test_close_channel_internally_closes_and_unregisters(self, loop):
        enter_loop(loop)
        channel = FakeChannel()

        loop._close_channel_internally(channel, "done")

        assert channel.socket().closed is True
        assert channel.close_future().done() is True
        assert channel.events == [("active", False, "done")]
        assert channel.unregister_calls == 1

    def test_events_to_str_formats_eventfd_known_and_unknown_channels(self, loop):
        client = FakeChannel(100)
        server = FakeChannel(101, server=True)
        loop._channels = {100: client, 101: server}
        events = [
            (selector_key(loop._eventfd), selectors.EVENT_READ),
            (selector_key(client), selectors.EVENT_WRITE),
            (selector_key(server), selectors.EVENT_READ),
            (selectors.SelectorKey(999, 999, 0, None), selectors.EVENT_READ),
        ]

        result = loop._events_to_str(events)

        assert "EventFD(" in result
        assert "client(100/channel-100):W" in result
        assert "server(101/channel-101):R" in result
        assert "unknown(999):R" in result

    def test_show_debug_info_handles_channels_with_pending_chunks(self, loop, caplog):
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        channel = FakeChannel()
        channel._pendings = [Chunk(b"abc"), Chunk(b"de")]
        idle = FakeChannel(101)
        server = FakeChannel(102, server=True)
        loop._channels[channel.fileno()] = channel
        loop._channels[idle.fileno()] = idle
        loop._channels[server.fileno()] = server

        loop._show_debug_info()

        assert "2 chunks, 5 bytes in total" in caplog.text

    def test_start_submits_start_function_and_waits_for_barrier(self, loop):
        loop._start_barrier.clear()
        submitted = []

        def submit(fn):
            submitted.append(fn)
            loop._start_barrier.set()
            return Future()

        loop._pool.submit = submit

        loop.start()

        assert submitted == [loop._start]


class TestTimeoutAndPolling:

    def test_millis_to_wait_for_connect_timeout(self, loop, monkeypatch):
        monkeypatch.setattr(eventloop_module.time, "time", lambda: 10.0)

        assert loop._millis_to_wait_for_connect_timeout() == -1

        loop._connect_timeout_due_millis = {1: 12050, 2: 13000}
        assert loop._millis_to_wait_for_connect_timeout() == 2050

        loop._connect_timeout_due_millis = {1: 9000}
        assert loop._millis_to_wait_for_connect_timeout() == 0

    def test_poll_timeout_non_debug(self, loop, monkeypatch):
        monkeypatch.setattr(loop, "_millis_to_wait_for_connect_timeout", lambda: -1)
        assert loop._poll_timeout() is None

        monkeypatch.setattr(loop, "_millis_to_wait_for_connect_timeout", lambda: 500)
        assert loop._poll_timeout() == 1

        monkeypatch.setattr(loop, "_millis_to_wait_for_connect_timeout", lambda: 2500)
        assert loop._poll_timeout() == 2

    def test_poll_timeout_debug_caps_to_debug_interval(self, loop, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        monkeypatch.setattr(eventloop_module, "DEBUG_INTERVAL_MILLIS", 3000)
        monkeypatch.setattr(loop, "_millis_to_wait_for_connect_timeout", lambda: -1)
        assert loop._poll_timeout() == 3

        monkeypatch.setattr(loop, "_millis_to_wait_for_connect_timeout", lambda: 5000)
        assert loop._poll_timeout() == 3

    def test_poll_selects_events_and_shows_debug_info_on_timeout(self, loop, caplog):
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        loop._selector.select_events = [[]]

        assert loop._poll() == []
        assert loop._selector.select_timeouts
        assert "counters" in caplog.text

    def test_poll_logs_infinity_timeout_and_polled_events_in_debug(self, loop, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        channel = FakeChannel()
        loop._channels[channel.fileno()] = channel
        loop._selector.select_events = [[(selector_key(channel), selectors.EVENT_READ)]]
        monkeypatch.setattr(loop, "_poll_timeout", lambda: None)

        assert loop._poll() == [(selector_key(channel), selectors.EVENT_READ)]
        assert "poll timeout: infinity" in caplog.text
        assert "events polled:" in caplog.text

    def test_process_connection_timeout_closes_inactive_due_channels(self, loop, monkeypatch):
        enter_loop(loop)
        monkeypatch.setattr(eventloop_module.time, "time", lambda: 10.0)
        due = FakeChannel(100)
        active = FakeChannel(101)
        active._ever_active = True
        future = FakeChannel(102)
        loop._channels = {100: due, 101: active, 102: future}
        loop._connect_timeout_due_millis = {100: 9999, 101: 9999, 102: 11000}

        loop._process_connection_timeout()

        assert due.socket().closed is True
        assert active.socket().closed is False
        assert future.socket().closed is False
        assert loop._connect_timeout_due_millis == {102: 11000}

    def test_process_connection_timeout_logs_countdowns_in_debug(self, loop, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        monkeypatch.setattr(eventloop_module.time, "time", lambda: 10.0)
        loop._connect_timeout_due_millis = {100: 11000}

        loop._process_connection_timeout()

        assert "checking connection timeout, countdowns:" in caplog.text

    def test_check_channel_active_uses_so_error_for_first_active(self, loop):
        peer = FakeChannel(101)
        loop._connect_timeout_due_millis[101] = 1
        assert loop._check_channel_active(peer) is True
        assert peer.events == [("active", True, "first time to be active")]
        assert peer.channel_future().done() is True
        assert 101 not in loop._connect_timeout_due_millis

        assert loop._check_channel_active(peer) is True
        assert peer.events == [("active", True, "first time to be active")]

    def test_check_channel_active_closes_on_connect_error(self, loop):
        enter_loop(loop)
        channel = FakeChannel(sock=FakeSocket(connect_error=errno.ECONNREFUSED))
        loop._connect_timeout_due_millis[channel.fileno0()] = 1

        assert loop._check_channel_active(channel) is False

        assert channel.socket().closed is True
        assert channel.close_future().done() is True
        assert channel.unregister_calls == 1
        assert channel.events[0][0] == "active"
        assert channel.events[0][1] is False
        assert channel.events[0][2].startswith("connect failed:")
        assert channel.fileno0() not in loop._connect_timeout_due_millis
        with pytest.raises(OSError) as exc_info:
            channel.channel_future().sync()
        assert exc_info.value.errno == errno.ECONNREFUSED

    def test_check_channel_active_closes_when_so_error_check_fails(self, loop):
        enter_loop(loop)
        failure = OSError("bad status")
        channel = FakeChannel(sock=FakeSocket(connect_error_exception=failure))

        assert loop._check_channel_active(channel) is False

        assert channel.socket().closed is True
        assert channel.close_future().done() is True
        with pytest.raises(OSError, match="bad status"):
            channel.channel_future().sync()


class TestStartLoop:

    def run_start_once(self, loop, events):
        calls = {"count": 0}

        def fake_poll():
            calls["count"] += 1
            loop._stop_polling = True
            return events

        loop._poll = fake_poll
        loop._start()

    def test_start_processes_eventfd_and_unknown_channel(self, loop, caplog):
        caplog.set_level(logging.DEBUG, logger="py_netty.eventloop")
        unknown_key = selectors.SelectorKey(999, 999, 0, None)

        self.run_start_once(loop, [
            (selector_key(loop._eventfd), selectors.EVENT_READ),
            (unknown_key, selectors.EVENT_READ),
        ])

        assert loop._eventfd.reads == 1
        assert loop._eventfd_read_count == 1
        assert loop._selector.closed is True

    def test_start_accepts_server_connections(self, loop):
        server = FakeChannel(100, server=True)
        server.accept_results = [
            (FakeSocket(201), ("127.0.0.1", 201)),
            (FakeSocket(202), ("127.0.0.1", 202)),
        ]
        loop._channels[100] = server

        self.run_start_once(loop, [(selector_key(server), selectors.EVENT_READ)])

        assert loop._total_accepted == 2
        assert server.events[0] == ("active", True, "server channel is always active")
        assert server.events[1][0] == "read"
        assert server.events[2][0] == "read"

    def test_start_accepts_server_connections_without_reactivating_active_server(self, loop):
        server = FakeChannel(100, server=True)
        server._ever_active = True
        server.accept_results = [(FakeSocket(201), ("127.0.0.1", 201))]
        loop._channels[100] = server

        self.run_start_once(loop, [(selector_key(server), selectors.EVENT_READ)])

        assert server.events == [("read", server.accept_results[0][0])]

    def test_start_removes_write_flag_when_client_has_no_pending(self, loop):
        channel = FakeChannel(100)
        loop._channels[100] = channel

        self.run_start_once(loop, [(selector_key(channel), selectors.EVENT_WRITE)])

        assert channel.removed_flags == [selectors.EVENT_WRITE]
        assert channel.writability_checks == 1

    def test_start_sends_pending_chunks_and_completes_futures(self, loop):
        channel = FakeChannel(100)
        first = Chunk(b"abc")
        second = Chunk(b"de")
        channel._pendings = [first, second]
        channel._pending_bytes = 5
        loop._channels[100] = channel

        self.run_start_once(loop, [(selector_key(channel), selectors.EVENT_WRITE)])

        assert first.future.done() is True
        assert second.future.done() is True
        assert channel.pendings() == []
        assert channel._pending_bytes == 0
        assert loop._total_sent == 5
        assert channel.removed_flags == [selectors.EVENT_WRITE]

    def test_start_keeps_partially_sent_chunk_pending(self, loop):
        channel = FakeChannel(100)
        chunk = Chunk(b"abc")
        channel._pendings = [chunk]
        channel._pending_bytes = 3
        channel.try_send_results = [b"bc"]
        loop._channels[100] = channel

        self.run_start_once(loop, [(selector_key(channel), selectors.EVENT_WRITE)])

        assert chunk.future.done() is False
        assert channel.pendings() == [chunk]
        assert chunk.buffer == b"bc"
        assert channel._pending_bytes == 2
        assert loop._total_sent == 1
        assert channel.removed_flags == []

    def test_start_closes_on_close_chunk(self, loop):
        enter_loop(loop)
        channel = FakeChannel(100)
        channel._pendings = [Chunk(b"", close=True)]
        loop._channels[100] = channel

        self.run_start_once(loop, [(selector_key(channel), selectors.EVENT_WRITE)])

        assert channel.socket().closed is True
        assert channel.unregister_calls == 1

    def test_start_reads_buffer_and_fires_read(self, loop):
        channel = FakeChannel(100)
        channel.recv_results = [(b"hello", False)]
        loop._channels[100] = channel

        self.run_start_once(loop, [(selector_key(channel), selectors.EVENT_READ)])

        assert loop._total_received == 5
        assert channel.events[-1] == ("read", b"hello")

    def test_start_ignores_empty_read_without_eof(self, loop):
        channel = FakeChannel(100)
        channel.recv_results = [(b"", False)]
        loop._channels[100] = channel

        self.run_start_once(loop, [(selector_key(channel), selectors.EVENT_READ)])

        assert loop._total_received == 0
        assert channel.socket().closed is False
        assert channel.events == []

    def test_start_closes_on_read_eof(self, loop):
        channel = FakeChannel(100)
        channel.recv_results = [(b"", True)]
        loop._channels[100] = channel

        self.run_start_once(loop, [(selector_key(channel), selectors.EVENT_READ)])

        assert channel.socket().closed is True
        assert channel.unregister_calls == 1

    def test_start_processes_tasks_and_connection_timeouts_after_events(self, loop):
        calls = []
        loop._taskq.put(lambda: calls.append("task"))
        channel = FakeChannel(100)
        loop._channels[100] = channel
        loop._connect_timeout_due_millis[100] = 0

        self.run_start_once(loop, [])

        assert calls == ["task"]
        assert channel.socket().closed is True


class TestEventLoopGroup:

    def test_get_eventloop_cycles_through_created_eventloops(self, monkeypatch):
        pool = FakePool(workers=2)
        created = []

        class DummyLoop:
            def __init__(self, pool):
                self.pool = pool
                self.stopped = False
                created.append(self)

            def stop(self):
                self.stopped = True

        monkeypatch.setattr(eventloop_module, "create_thread_pool", lambda num, prefix: pool)
        monkeypatch.setattr(eventloop_module, "EventLoop", DummyLoop)

        group = EventLoopGroup(2, "worker")

        assert len(created) == 2
        assert group.get_eventloop() is created[0]
        assert group.get_eventloop() is created[1]
        assert group.get_eventloop() is created[0]

        with group as entered:
            assert entered is group

        assert [loop.stopped for loop in created] == [True, True]
        assert pool.shutdown_called is True
