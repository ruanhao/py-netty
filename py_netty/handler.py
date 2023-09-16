import abc
import logging
from .utils import LoggerAdapter


logger = LoggerAdapter(logging.getLogger(__name__))


class AbstractChannelHandler(abc.ABC):

    @abc.abstractmethod
    def channel_active(self, ctx):
        pass

    @abc.abstractmethod
    def channel_read(self, ctx, buffer):
        pass

    @abc.abstractmethod
    def channel_inactive(self, ctx):
        pass


class ChannelHandlerAdapter(AbstractChannelHandler):
    def channel_active(self, ctx):
        pass

    def channel_read(self, ctx, buffer):
        pass

    def channel_inactive(self, ctx):
        pass


NoOpChannelHandler = ChannelHandlerAdapter
DefaultChannelHandler = ChannelHandlerAdapter


class LoggingChannelHandler(AbstractChannelHandler):
    def channel_active(self, ctx: 'ChannelContext'):
        logger.debug(f"[Channel Active] {ctx.channel()}")
        pass

    def channel_read(self, ctx, buffer):
        logger.debug(f"[Channel Read: {len(buffer)} bytes] {ctx.channel()}")
        pass

    def channel_inactive(self, ctx):
        logger.debug(f"[Channel Inactive] {ctx.channel()}")
        pass


class EchoChannelHandler(LoggingChannelHandler):

    def __init__(self):
        super().__init__()

    def channel_read(self, ctx: 'ChannelContext', buffer):
        super().channel_read(ctx, buffer)
        ctx.write(buffer)
