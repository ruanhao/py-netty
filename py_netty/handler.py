import abc
import logging
from .utils import LoggerAdapter
from typing import Union
import socket


logger = LoggerAdapter(logging.getLogger(__name__))


class AbstractChannelHandler(abc.ABC):

    @abc.abstractmethod
    def channel_active(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    @abc.abstractmethod
    def channel_read(self, ctx: 'ChannelHandlerContext', msg: Union[bytes, socket.socket]) -> None:
        pass

    @abc.abstractmethod
    def channel_inactive(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    @abc.abstractmethod
    def channel_registered(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    @abc.abstractmethod
    def channel_unregistered(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    @abc.abstractmethod
    def channel_handshake_complete(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    @abc.abstractmethod
    def exception_caught(self, ctx: 'ChannelHandlerContext', exception: Exception) -> None:
        pass


class ChannelHandlerAdapter(AbstractChannelHandler):

    def channel_active(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    def channel_read(self, ctx: 'ChannelHandlerContext', msg: Union[bytes, socket.socket]) -> None:
        pass

    def channel_inactive(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    def channel_registered(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    def channel_unregistered(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    def channel_handshake_complete(self, ctx: 'ChannelHandlerContext') -> None:
        pass

    def exception_caught(self, ctx: 'ChannelHandlerContext', exception: Exception) -> None:
        pass


NoOpChannelHandler = ChannelHandlerAdapter
DefaultChannelHandler = ChannelHandlerAdapter


class LoggingChannelHandler(AbstractChannelHandler):

    def channel_active(self, ctx: 'ChannelHandlerContext') -> None:
        logger.debug("[Channel Active] %s", ctx.channel())

    def channel_read(self, ctx: 'ChannelHandlerContext', msg: Union[bytes, socket.socket]) -> None:
        logger.debug("[Channel Read] %s", ctx.channel())

    def channel_inactive(self, ctx: 'ChannelHandlerContext') -> None:
        logger.debug("[Channel Inactive] %s", ctx.channel())

    def channel_registered(self, ctx: 'ChannelHandlerContext') -> None:
        logger.debug("[Channel Registered] %s", ctx.channel())

    def channel_unregistered(self, ctx: 'ChannelHandlerContext') -> None:
        logger.debug("[Channel Unregistered] %s", ctx.channel())

    def channel_handshake_complete(self, ctx: 'ChannelHandlerContext') -> None:
        logger.debug("[Channel Handshake Complete] %s", ctx.channel())

    def exception_caught(self, ctx: 'ChannelHandlerContext', exception: Exception) -> None:
        logger.error("[Exception Caught] %s : %s", ctx.channel(), str(exception), exc_info=exception)


class EchoChannelHandler(ChannelHandlerAdapter):

    def __init__(self):
        super().__init__()

    def channel_read(self, ctx: 'ChannelHandlerContext', msg: Union[bytes, socket.socket]) -> None:
        ctx.write(msg)
