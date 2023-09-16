from dataclasses import dataclass
from concurrent.futures import Future
import socket
import logging
from .utils import sockinfo

logger = logging.getLogger(__name__)


@dataclass
class ChannelFuture:

    future: Future = None

    def __post_init__(self):
        self.future = self.future or Future()

    def channel(self) -> 'AbstractChannel':
        return self.future.result()

    def is_done(self):
        return self.future.done()

    def set(self, channel: 'AbstractChannel'):
        self.future.set_result(channel)


@dataclass
class AbstractChannel:

    _eventloop: 'EventLoop'
    _socket: socket.socket

    def __post_init__(self):
        self._fileno = self._socket.fileno()

    def write(self, buffer):
        self._eventloop.write(self._fileno, buffer)

    def close_forcibly(self):
        logger.debug(f"Closing channel FORCIBLY: {self}")
        self._eventloop.close_forcibly(self._fileno)

    def close_on_complete(self):
        logger.debug(f"Closing channel GRACEFULLY: {self}")
        return self._eventloop.close_on_complete(self._fileno)

    def __str__(self):
        return sockinfo(self._socket)


class NioSocketChannel(AbstractChannel):

    def __init__(self, eventloop: 'EventLoop', socket: socket.socket):
        super().__init__(eventloop, socket)

    pass


class NioServerSocketChannel(AbstractChannel):

    def __init__(self, eventloop: 'EventLoop', socket: socket.socket):
        super().__init__(eventloop, socket)


@dataclass
class ChannelContext:
    _channel: AbstractChannel

    def close(self):
        self._channel.close()

    def write(self, buffer):
        self._channel.write(buffer)

    def channel(self):
        return self._channel
