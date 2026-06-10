import logging

import pytest

from py_netty.handler import (
    AbstractChannelHandler,
    ChannelHandlerAdapter,
    DefaultChannelHandler,
    EchoChannelHandler,
    LoggingChannelHandler,
    NoOpChannelHandler,
)


class DummyChannel:

    def __str__(self):
        return "dummy-channel"


class DummyContext:

    def __init__(self):
        self._channel = DummyChannel()
        self.writes = []

    def channel(self):
        return self._channel

    def write(self, msg):
        self.writes.append(msg)
        return "write-result"


def test_abstract_channel_handler_cannot_be_instantiated():
    with pytest.raises(TypeError):
        AbstractChannelHandler()


def test_default_handler_aliases_point_to_adapter():
    assert NoOpChannelHandler is ChannelHandlerAdapter
    assert DefaultChannelHandler is ChannelHandlerAdapter


class TestChannelHandlerAdapter:

    def test_callbacks_are_noops(self):
        handler = ChannelHandlerAdapter()
        ctx = DummyContext()
        exception = RuntimeError("boom")

        assert handler.channel_active(ctx) is None
        assert handler.channel_read(ctx, b"payload") is None
        assert handler.channel_inactive(ctx) is None
        assert handler.channel_registered(ctx) is None
        assert handler.channel_unregistered(ctx) is None
        assert handler.channel_handshake_complete(ctx) is None
        assert handler.channel_writability_changed(ctx) is None
        assert handler.exception_caught(ctx, exception) is None
        assert ctx.writes == []


class TestEchoChannelHandler:

    def test_channel_read_writes_bytes_back_to_context(self):
        handler = EchoChannelHandler()
        ctx = DummyContext()

        assert handler.channel_read(ctx, b"hello") is None

        assert ctx.writes == [b"hello"]

    def test_channel_read_writes_object_back_without_conversion(self):
        handler = EchoChannelHandler()
        ctx = DummyContext()
        msg = object()

        handler.channel_read(ctx, msg)

        assert ctx.writes == [msg]


class TestLoggingChannelHandler:

    @pytest.mark.parametrize(
        "method_name,args,label",
        [
            ("channel_active", (), "[Channel Active]"),
            ("channel_read", (b"payload",), "[Channel Read]"),
            ("channel_inactive", (), "[Channel Inactive]"),
            ("channel_registered", (), "[Channel Registered]"),
            ("channel_unregistered", (), "[Channel Unregistered]"),
            ("channel_handshake_complete", (), "[Channel Handshake Complete]"),
            ("channel_writability_changed", (), "[Channel Writability Changed]"),
        ],
    )
    def test_callbacks_log_channel_events(self, caplog, method_name, args, label):
        handler = LoggingChannelHandler()
        ctx = DummyContext()
        caplog.set_level(logging.DEBUG, logger="py_netty.handler")

        getattr(handler, method_name)(ctx, *args)

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert label in messages
        assert "dummy-channel" in messages

    def test_exception_caught_logs_error_with_exception(self, caplog):
        handler = LoggingChannelHandler()
        ctx = DummyContext()
        exception = RuntimeError("boom")
        caplog.set_level(logging.ERROR, logger="py_netty.handler")

        handler.exception_caught(ctx, exception)

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "[Exception Caught]" in messages
        assert "dummy-channel" in messages
        assert "boom" in messages
        assert any(record.exc_info for record in caplog.records)
