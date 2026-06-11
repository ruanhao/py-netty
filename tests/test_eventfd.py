import os
import select

import pytest

from py_netty.eventfd import PipeEventFD, SocketEventFD, eventfd


def _is_readable(fd, timeout=0):
    return select.select([fd], [], [], timeout)[0] == [fd]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows select() only supports sockets; eventfd() uses SocketEventFD there.",
)
class TestPipeEventFD:

    def test_set_clear_and_wait(self):
        event = PipeEventFD()

        assert event.is_set() is False
        assert event.wait(0) is False
        assert _is_readable(event) is False

        event.set()
        assert event.is_set() is True
        assert event.wait(0) is True
        assert _is_readable(event, 0.5) is True

        event.set()
        event.clear()
        assert event.is_set() is False
        assert event.wait(0) is False
        assert _is_readable(event) is False

        event.clear()
        assert event.is_set() is False

    def test_unsafe_write_coalesces_until_read(self):
        event = PipeEventFD()

        event.unsafe_write()
        event.unsafe_write()

        assert _is_readable(event, 0.5) is True

        event.unsafe_read()
        assert _is_readable(event) is False

        event.unsafe_write()
        assert _is_readable(event, 0.5) is True

        event.unsafe_read()
        assert _is_readable(event) is False


class TestSocketEventFD:

    def test_fileno_returns_read_socket_fd(self):
        event = SocketEventFD()

        assert event.fileno() == event._read_fd.fileno()

    def test_set_clear_and_select(self):
        event = SocketEventFD()

        assert event.is_set() is False
        assert _is_readable(event) is False

        event.set()
        assert event.is_set() is True
        assert _is_readable(event, 0.5) is True

        event.clear()
        assert event.is_set() is False
        assert _is_readable(event) is False


def test_eventfd_factory_uses_platform_specific_implementation():
    event = eventfd()

    if os.name == "nt":
        assert isinstance(event, SocketEventFD)
    else:
        assert isinstance(event, PipeEventFD)


def test_eventfd_close_is_idempotent():
    event = eventfd()

    event.close()
    event.close()

    assert event.fileno() == -1
