from dataclasses import dataclass, field
from concurrent.futures import Future
import socket
import select
import logging
from .utils import sockinfo, log
from .handler import LoggingChannelHandler
from typing import Callable, List
from .bytebuf import Chunk, EMPTY_BUFFER


logger = logging.getLogger(__name__)


@dataclass
class AbstractChannel:

    _eventloop: 'EventLoop'
    _socket: socket.socket
    _handler_initializer: Callable = field(default_factory=LoggingChannelHandler)

    def __post_init__(self):
        self._fileno = self._socket.fileno()
        assert self._fileno > 0
        self._close_future = ChannelFuture()
        self._active = False
        self._handler = None    # lazy initialization
        self._flag = 0          # intrested events
        self._server_channel = False
        self._ever_active = False
        self.__str__()          # load sockinfo

    def id(self):
        return str(hex(id(self.socket())))

    def context(self) -> 'ChannelContext':
        return ChannelContext(self)

    def set_flag(self, flag):
        self._flag = flag

    def socket(self) -> socket.socket:
        return self._socket

    def register(self) -> 'ChannelFuture':
        return self._eventloop.register(self)

    def unregister(self) -> 'ChannelFuture':
        return self._eventloop.unregister(self)

    def flag(self):
        return self._flag

    def add_flag(self, flag):
        if self._flag & flag:
            return
        self._flag |= flag
        logger.debug("add flag %s to channel %s, current flag: %s", flag, self.id(), self._flag)
        try:
            self._eventloop._epoll.modify(self._fileno, self._flag)
        except Exception:       # maybe fileno is closed
            logger.exception("add flag %s to channel %s failed", flag, self.id())

    def remove_flag(self, flag):
        if not self._flag & flag:
            return
        self._flag &= ~flag
        logger.debug("remove flag %s from channel %s, current flag: %s", flag, self.id(), self._flag)
        try:
            self._eventloop._epoll.modify(self._fileno, self._flag)
        except Exception:       # maybe fileno is closed
            logger.exception("remove flag %s from channel %s failed", flag, self.id())

    def handler(self):
        if self._handler is None:
            self._handler = self._handler_initializer()
        return self._handler

    def is_server(self):
        """Returns True if this channel is related to a server listening socket, False otherwise."""
        return self._server_channel

    def fileno(self):
        return self._socket.fileno()

    def fileno0(self) -> int:
        """origin fileno"""
        return self._fileno

    def close_future(self) -> 'ChannelFuture':
        return self._close_future

    def is_active(self):
        if self.close_future().done():
            return False
        return self._active

    @log(logger)
    def set_active(self, active, reason=''):
        origin = self._active
        if origin != active:
            logger.debug("set channel %s active status: %s, reason: %s", self.id(), active, reason)
        self._active = active
        if origin is True and active is False:
            self.handler().channel_inactive(self.context())
        if origin is False and active is True:
            self._ever_active = True
            self.handler().channel_active(self.context())

    def close(self, force=False):
        if force:
            self.close_forcibly()
        else:                   # gracefully
            self.close_gracefully()

    def close_forcibly(self) -> 'ChannelFuture':
        if not self.in_eventloop():
            self._eventloop.submit_task(self.close_forcibly)
            return self.close_future()
        logger.debug(f"Closing channel FORCIBLY: {self}")
        self._eventloop._close_channel_internally(self, 'close channel forcibly')
        return self.close_future()

    def close_gracefully(self) -> 'ChannelFuture':
        if not self.in_eventloop():
            self._eventloop.submit_task(self.close_gracefully)
            return self.close_future()

        logger.debug(f"Closing channel (active:{self.is_active()}) GRACEFULLY: {self}")
        if not self.is_active():
            return self.close_future()

        if self.is_server():
            self._eventloop._close_channel_internally(self, 'close server channel gracefully')
        else:                  # client channel
            self.add_pending(Chunk(EMPTY_BUFFER, self.close_future().future, True))
        return self.close_future()

    def in_eventloop(self):
        return self._eventloop.in_eventloop()

    def __str__(self):
        if not hasattr(self, '_sockinfo'):
            self._sockinfo = sockinfo(self._socket)
        if not self._ever_active:
            return self._sockinfo.replace('-', '?')
        if not self.is_active():
            return self._sockinfo.replace('-', '!')
        return self._sockinfo


class NioSocketChannel(AbstractChannel):

    def __init__(self, eventloop: 'EventLoop', sock: socket.socket, handler_initializer: Callable, connect_timeout_millis: int = 3000):
        super().__init__(eventloop, sock, handler_initializer)
        self._pendings = []     # [Chunk, ...]
        self.socket().setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._connect_timeout_millis = connect_timeout_millis

    def connect_timeout_millis(self) -> int:
        return self._connect_timeout_millis

    def pendings(self) -> List['Chunk']:
        return self._pendings

    def set_pendings(self, pendings: List['Chunk']):
        self._pendings = pendings
        self.add_flag(select.POLLOUT)

    def add_pending(self, chunk: 'Chunk'):
        if chunk is None:
            return
        if chunk.close is False and not chunk.buffer:
            return
        self._pendings.append(chunk)
        self.add_flag(select.POLLOUT)

    def has_pendings(self) -> bool:
        return len(self._pendings) > 0

    def write(self, buffer, channel_future: 'ChannelFuture' = None) -> 'ChannelFuture':
        cf = channel_future or ChannelFuture()
        if not self.in_eventloop():
            self._eventloop.submit_task(lambda: self.write(buffer, cf))
            return cf
        self.add_pending(Chunk(buffer, cf.future))
        return cf

    def try_send(self, bytebuf: bytes, spin=1) -> bytes:
        if not bytebuf:
            return b''
        total_sent = 0
        while total_sent < len(bytebuf):
            try:
                total_sent += self.socket().send(bytebuf[total_sent:])
            except socket.error:
                if spin > 0:
                    spin -= 1
                    continue
                break
        return bytebuf[total_sent:]

    def recvall(self) -> bytes:
        buffer = b''
        while True:
            try:
                received = self.socket().recv(1024)
                if not received:  # EOF
                    return buffer
                buffer += received
            except socket.error:
                return buffer
        pass


class NioServerSocketChannel(AbstractChannel):

    def __init__(self, eventloop: 'EventLoop', sock: socket.socket, handler_initializer: Callable):
        super().__init__(eventloop, sock, handler_initializer)
        self._server_channel = True

    def acceptall(self) -> list:  # [(socket, address), ...]
        result = []
        while True:
            try:
                result.append(self.socket().accept())
            except socket.error:
                return result


@dataclass
class ChannelContext:
    _channel: AbstractChannel

    def close(self):
        self._channel.close()

    def write(self, buffer):
        self._channel.write(buffer)

    def channel(self):
        return self._channel


@dataclass
class ChannelFuture:

    future: Future = None

    def __post_init__(self):
        self.future = self.future or Future()

    def channel(self) -> AbstractChannel:
        return self.future.result()

    def close_future(self) -> 'ChannelFuture':
        return self.channel().close_future()

    def sync(self) -> 'ChannelFuture':
        self.future.result()
        return self

    def done(self) -> bool:
        return self.future.done()

    def set(self, channel: AbstractChannel) -> None:
        if self.future.done():
            return
        self.future.set_result(channel)

    def add_listener(self, listener: Callable) -> None:
        self.future.add_done_callback(lambda f: listener(self))
