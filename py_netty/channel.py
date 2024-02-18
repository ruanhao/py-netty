import os
import ssl
import time
import socket
import logging
from functools import wraps
from concurrent.futures import Future
from typing import Callable, List, Union, Tuple, Optional
from .bytebuf import Chunk, EMPTY_BUFFER
from .handler import LoggingChannelHandler
from .utils import sockinfo, log, LoggerAdapter, flag_to_str
import selectors
from attrs import define, field

logger = LoggerAdapter(logging.getLogger(__name__))

_ROUNDS = int(os.getenv('PY_NETTY_TUNING_ROUNDS', 16))
_INITIAL_BUFFER_SIZE = int(os.getenv('PY_NETTY_TUNING_INIT_BUFFER_SIZE', 1024))
_MIN_BUFFER_SIZE = int(os.getenv('PY_NETTY_TUNING_MIN_BUFFER_SIZE', _INITIAL_BUFFER_SIZE >> 1))
_MAX_BUFFER_SIZE = int(os.getenv('PY_NETTY_TUNING_MAX_BUFFER_SIZE', _INITIAL_BUFFER_SIZE << 4))


@define(slots=True, kw_only=True)
class ChannelInfo:

    sock: socket.socket = field()
    id: str = field()
    sockname: Tuple[str, int] = field()
    peername: Tuple[str, int] = field()
    fileno: int = field(default=-1)

    @classmethod
    def of(cls, sock: socket.socket):
        return cls(
            sock=sock,
            id=hex(id(sock)),
            sockname=sock.getsockname()[:2],
            peername=sock.getpeername()[:2],
            fileno=sock.fileno()
        )


def adaptive_bufsize(previous_bufsize, data_size):
    if data_size < (previous_bufsize >> 1) and previous_bufsize > _MIN_BUFFER_SIZE:
        return max(previous_bufsize >> 1, _MIN_BUFFER_SIZE)
    elif data_size == previous_bufsize and previous_bufsize < _MAX_BUFFER_SIZE:
        return min(previous_bufsize << 1, _MAX_BUFFER_SIZE)
    else:
        return previous_bufsize


@define(slots=True)
class AbstractChannel:

    _eventloop: 'EventLoop' = field()
    _socket: socket.socket = field()
    _handler_initializer: Callable = field(factory=LoggingChannelHandler)

    def __attrs_post_init__(self):
        self._fileno = self._socket.fileno()
        assert self._fileno > 0
        self._close_future = ChannelFuture(self)
        self._active = False
        self._handler = None    # lazy initialization
        self._flag = 0          # interested events
        self._server_channel = False
        self._ever_active = False
        self._sockinfo = None
        self._channelinfo = None
        self._channel_future = ChannelFuture(self)

    def channel_future(self) -> 'ChannelFuture':
        return self._channel_future

    def eventloop(self) -> 'EventLoop':
        return self._eventloop

    def id(self):
        return str(hex(id(self.socket())))

    def context(self) -> 'ChannelContext':
        return ChannelContext(self)

    def set_flag(self, flag):
        self._flag = flag

    def socket(self) -> socket.socket:
        return self._socket

    def channelinfo(self) -> Optional[ChannelInfo]:
        """Include ORIGINAL socket info, even if the sock is closed."""
        if self._channelinfo is None:
            _ = str(self)
        return self._channelinfo

    def register(self) -> 'ChannelFuture':
        return self.eventloop().register(self)

    def unregister(self) -> 'ChannelFuture':
        return self.eventloop().unregister(self)

    def flag(self):
        return self._flag

    def add_flag(self, flag):
        if self._flag & flag:
            return
        self._flag |= flag
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("add flag %s(%s) to channel %s/%s, current flag: %s(%s)",
                         flag, flag_to_str(flag), self.id(), self._fileno, self._flag, flag_to_str(self._flag))
        try:
            self.eventloop().modify_flag(self._fileno, self._flag)
        except KeyError:
            pass
        except Exception:       # maybe fileno is closed
            logger.exception("add flag %s(%s) to channel %s/%s failed", flag, flag_to_str(flag), self.id(), self._fileno)

    def remove_flag(self, flag):
        if not self._flag & flag:
            return
        self._flag &= ~flag
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("remove flag %s(%s) from channel %s/%s, current flag: %s(%s)",
                         flag, flag_to_str(flag), self.id(), self._fileno, self._flag, flag_to_str(self._flag))
        try:
            self.eventloop().modify_flag(self._fileno, self._flag)
        except Exception:       # maybe fileno is closed
            logger.exception("remove flag %s(%s) from channel %s failed", flag, flag_to_str(flag), self.id())

    def handler(self):
        if self._handler is None:
            self._handler = self._handler_initializer()
        return self._handler

    def handler_context(self) -> 'ChannelHandlerContext':
        return ChannelHandlerContext(self)

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

    def _refresh_sock_info(self) -> str:
        self._sockinfo = None
        self._channelinfo = None
        return str(self)

    @log(logger)
    def set_active(self, active, reason=''):
        origin = self._active
        if origin != active:
            logger.debug("set channel %s active status: %s, reason: %s", self.id(), active, reason)
        self._active = active
        if origin is True and active is False:
            self.handler_context().fire_channel_inactive()
        if origin is False and active is True:
            self._ever_active = True
            self._refresh_sock_info()
            if isinstance(self.socket(), ssl.SSLSocket):
                try:
                    s = time.perf_counter()
                    self.socket().do_handshake(True)
                    cost = time.perf_counter() - s  # seconds
                    if cost > 1:
                        logger.warning("ssl handshake cost: ~%ss", round(cost, 2))
                except socket.timeout:
                    logger.exception("ssl handshake timeout")
                    self.close(True)
                except socket.error as socket_err:
                    logger.debug("ssl handshake error: %s", str(socket_err))
                    # if errno.ENOTCONN is not socket_err.errno:
                    #     logger.debug("ssl handshake error: %s", str(socket_err))
                else:
                    self.handler_context().fire_channel_handshake_complete()
                # finally:
                #     with suppress(Exception):
                #         self.socket().settimeout(0)

            self.handler_context().fire_channel_active()

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
        self.eventloop()._close_channel_internally(self, 'close channel forcibly')
        return self.close_future()

    def close_gracefully(self) -> 'ChannelFuture':
        if not self.in_eventloop():
            self._eventloop.submit_task(self.close_gracefully)
            return self.close_future()

        logger.debug(f"Closing channel (active:{self.is_active()}) GRACEFULLY: {self}")
        if not self.is_active():
            return self.close_future()

        if self.is_server():
            self.eventloop()._close_channel_internally(self, 'close server channel gracefully')
        else:                  # client channel
            self.add_pending(Chunk(EMPTY_BUFFER, self.close_future().future, True))
        return self.close_future()

    def in_eventloop(self):
        return self._eventloop.in_eventloop()

    def __str__(self):
        if not self._sockinfo:
            self._sockinfo = sockinfo(self._socket)
        if not self._channelinfo:
            try:
                self._channelinfo = ChannelInfo.of(self._socket)
            except Exception:
                pass
        if self._channelinfo:
            ci = self._channelinfo
            l_addr = ':'.join(map(str, ci.sockname))
            r_addr = ':'.join(map(str, ci.peername))
            sign = '-'
            if not self._ever_active:
                sign = '?'
            elif not self.is_active():
                sign = '!'
            return f"[id:{ci.id}, fd:{ci.fileno}, L:/{l_addr} {sign} R:/{r_addr}]"
        if not self._ever_active:
            return self._sockinfo.replace('-', '?')
        if not self.is_active():
            return self._sockinfo.replace('-', '!')
        return self._sockinfo


class NioSocketChannel(AbstractChannel):

    def __init__(self, eventloop: 'EventLoop', sock: socket.socket, handler_initializer: Callable, connect_timeout_millis: int = 3000):
        super().__init__(eventloop, sock, handler_initializer)
        self._pendings = []     # [Chunk, ...]
        try:
            self.socket().setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            logger.exception("setsockopt TCP_NODELAY failed")
        self._connect_timeout_millis = connect_timeout_millis

    def connect_timeout_millis(self) -> int:
        return self._connect_timeout_millis

    def pendings(self) -> List['Chunk']:
        return self._pendings

    def set_pendings(self, pendings: List['Chunk']):
        self._pendings = pendings
        self.add_flag(selectors.EVENT_WRITE)

    def add_pending(self, chunk: 'Chunk'):
        if chunk is None:
            return
        if chunk.close is False and not chunk.buffer:
            return
        self._pendings.append(chunk)
        self.add_flag(selectors.EVENT_WRITE)

    def has_pendings(self) -> bool:
        return len(self._pendings) > 0

    def write(self, buffer, channel_future: 'ChannelFuture' = None) -> 'ChannelFuture':
        cf = channel_future or ChannelFuture(self)
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
            except socket.error as socket_err:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("try_send socket.error: %s, spin: %s", str(socket_err), spin)
                if spin > 0:
                    spin -= 1
                    continue
                break
        return bytebuf[total_sent:]

    def recvall(self) -> (bytes, bool):
        # if isinstance(self.socket(), ssl.SSLSocket):
        #     return self.recvall_ssl()
        buffer = b''
        bufsize = _INITIAL_BUFFER_SIZE
        rounds = 0
        total_costs = 0
        while True:
            rounds += 1
            try:
                s = time.perf_counter()
                received = self.socket().recv(bufsize)
                cost = time.perf_counter() - s  # seconds
                total_costs += cost
                if not received:  # EOF
                    return buffer, True
                recv_len = len(received)
                bufsize = adaptive_bufsize(bufsize, recv_len)
                buffer += received
                if rounds == _ROUNDS or total_costs > 0.1 or cost > 0.01:
                    if logger.isEnabledFor(logging.DEBUG):
                        cost = round(cost, 3)
                        total_costs = round(total_costs, 3)
                        sock_info = sockinfo(self.socket())
                        logger.debug(f"yield from recvall, rounds:{rounds}, current cost:{cost * 1000}ms, total cost:{total_costs * 1000}ms, bufsize:{bufsize}, buffer: {len(buffer)}, socket:{sock_info}")
                    return buffer, False
            # except ssl.SSLWantReadError:  # for ssl socket
            #     logger.debug("recvall ssl.SSLWantReadError, readable: %s", self.is_readable())
            #     if self.is_readable():
            #         continue
            #     return buffer, False
            except socket.error as socket_err:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("recvall socket.error: %s, readable: %s", str(socket_err), self.is_readable())
                if self.is_readable():
                    continue
                return buffer, False

    def is_readable(self) -> bool:
        try:
            return not self.socket().recv(16, socket.MSG_DONTWAIT | socket.MSG_PEEK) == b''
        except Exception:
            return False

    # def recvall_ssl(self) -> (bytes, bool):
    #     buffer = b''
    #     while True:
    #         try:
    #             received = self.socket().recv(1024)
    #             buffer += received
    #             if not received:  # EOF
    #                 return buffer, True
    #         except ssl.SSLWantReadError:
    #             if self.is_readable():
    #                 continue
    #             return buffer, False
    #         except socket.error:
    #             return buffer, False


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


@define(slots=True)
class ChannelContext:
    _channel: AbstractChannel = field()

    def close(self):
        self._channel.close()

    def write(self, buffer):
        self._channel.write(buffer)

    def channel(self):
        return self._channel


# annotation
def _catch_exception(func):
    @wraps(func)
    def inner(self, *args, **kwargs):
        try:
            func(self, *args, **kwargs)
        except Exception as e:
            self.fire_exception_caught(e)
    return inner


@define(slots=True)
class ChannelHandlerContext:

    _channel: AbstractChannel = field()

    def close(self):
        self._channel.close()

    def write(self, bytebuf) -> 'ChannelFuture':
        return self._channel.write(bytebuf)

    def channel(self):
        return self._channel

    def handler(self):
        return self._channel.handler()

    def fire_exception_caught(self, exception):
        try:
            self.handler().exception_caught(self, exception)
        except Exception:
            logger.exception(f"Exception caught while handling exception: {exception}")

    @_catch_exception
    def fire_channel_registered(self):
        self.handler().channel_registered(self)

    @_catch_exception
    def fire_channel_unregistered(self):
        self.handler().channel_unregistered(self)

    @_catch_exception
    def fire_channel_read(self, msg: Union[bytes, socket.socket]):
        self.handler().channel_read(self, msg)

    @_catch_exception
    def fire_channel_active(self):
        self.handler().channel_active(self)

    @_catch_exception
    def fire_channel_inactive(self):
        self.handler().channel_inactive(self)

    @_catch_exception
    def fire_channel_handshake_complete(self):
        self.handler().channel_handshake_complete(self)


@define(slots=True)
class ChannelFuture:

    _channel: AbstractChannel = field()
    future: Future = field(default=None)

    def __attrs_post_init__(self):
        self.future = self.future or Future()

    def channel(self) -> AbstractChannel:
        return self._channel

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
