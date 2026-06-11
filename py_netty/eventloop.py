import queue
import time
import itertools
import selectors
import logging
import socket
import ssl
import threading
from .eventfd import eventfd
from concurrent.futures import ThreadPoolExecutor
from .utils import create_thread_pool, sockinfo, log, LoggerAdapter, flag_to_str
from .channel import ChannelFuture, AbstractChannel
from typing import List, Tuple
import os
import inspect
from attrs import define, field

logger = LoggerAdapter(logging.getLogger(__name__))

DEBUG_INTERVAL_MILLIS = int(os.getenv('PY_NETTY_DEBUG_INTERVAL_MILLIS', 60000))


class EventLoop:

    def __init__(self, pool: ThreadPoolExecutor):
        assert pool, "thread pool executor is required"

        # internals
        self._channels = {}  # {fileno: Channel}
        self._connect_timeout_due_millis = {}  # {fileno: due_millis}
        self._thread = None
        self._stop_polling = False
        self._closed = False
        self._start_barrier = threading.Event()
        self._lock = threading.Lock()
        self._pool = pool

        # create selector
        self._eventfd = eventfd()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._eventfd, selectors.EVENT_READ)
        logger.debug("selector(%s) created for pool [%s]", type(self._selector).__name__, self._pool._thread_name_prefix)

        # queues
        self._taskq = queue.Queue()

        # counters
        self._eventfd_read_count = 0
        self._eventfd_write_count = 0
        self._total_accepted = 0
        self._total_sent = 0
        self._total_received = 0
        self._total_registered = 0
        self._total_tasks_submitted = 0
        self._total_tasks_processed = 0

    def modify_flag(self, channel):
        if not self.in_eventloop():
            self.submit_task(lambda: self.modify_flag(channel))
            return

        fileno = channel._fileno
        flag = channel._flag

        if flag == 0:
            self._selector.unregister(fileno)
            return

        try:
            key = self._selector.get_key(fileno)
        except KeyError:
            if channel.socket().fileno() == -1:
                return
            self._selector.register(channel, flag)
        else:
            if key.events == flag:
                return
            self._selector.modify(fileno, flag)

    def submit_task(self, task):
        if self._stop_polling or self._closed:
            raise RuntimeError("event loop is stopping")
        self.start()
        self._taskq.put(task)
        self._total_tasks_submitted += 1
        self.interrupt("submit task")

    def interrupt(self, desc=""):
        if desc and logger.isEnabledFor(logging.DEBUG):
            thread_name = self._thread.name if self._thread else "not-started"
            logger.debug(f"interrupting eventloop with EventFD {hex(id(self._eventfd))} in {thread_name}: {desc}")
        try:
            self._eventfd.unsafe_write()
        except Exception:
            logger.exception("failed to interrupt event loop")
            return

        if not logger.isEnabledFor(logging.DEBUG):
            return
        # only in debug mode to accumulate counter
        with self._lock:
            self._eventfd_write_count += 1

    def unregister(self, channel: AbstractChannel, channel_future: ChannelFuture = None):
        cf = channel_future or ChannelFuture(channel)
        if not self.in_eventloop():
            self.submit_task(lambda: self.unregister(channel, cf))
            return cf
        fileno = channel.fileno0()
        try:
            self._selector.unregister(fileno)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("unregistered channel %s/%s from selector", channel.id(), fileno)
            channel.handler_context().fire_channel_unregistered()
        except Exception as e:
            logger.debug("channel %s/%s was not unregistered cleanly: %s", channel.id(), fileno, e)
        self._channels.pop(fileno, None)
        self._connect_timeout_due_millis.pop(fileno, None)
        cf.set(channel)
        return cf

    def in_eventloop(self):
        return self._thread == threading.current_thread()

    def register(self, channel: AbstractChannel, only_write=False) -> ChannelFuture:
        self.start()

        if not self.in_eventloop():
            self.submit_task(lambda: self.register(channel, only_write))
            return channel.channel_future()

        if channel.socket().fileno() == -1:
            # channel closed already
            return channel.channel_future()

        channel.socket().setblocking(False)

        if only_write:
            flag = selectors.EVENT_WRITE
        else:
            flag = selectors.EVENT_READ | selectors.EVENT_WRITE
        channel.set_flag(flag)
        self._selector.register(channel, flag)
        channel.handler_context().fire_channel_registered()
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("registered channel(server:%s) [%s/%s] with flag: %s(%s)",
                         channel.is_server(), channel.id(), channel.fileno(), flag, flag_to_str(flag))
        self._total_registered += 1
        self._channels[channel.fileno()] = channel
        if not channel.is_server():
            self._connect_timeout_due_millis[channel.fileno()] = int(time.time() * 1000) + channel.connect_timeout_millis()

        # cf.set(channel)
        return channel.channel_future()

    def stop(self):
        logger.debug("stopping poll")
        if self._closed or self._stop_polling:
            return
        self._stop_polling = True
        if not self._start_barrier.is_set():
            self._cleanup_resources('event loop stopped before start')
            return
        self.interrupt('stop poll')

    def _process_task_queue(self):
        while not self._taskq.empty():
            task = self._taskq.get()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("task to run: %s", task)
            start = time.time()
            try:
                task()
            except Exception:
                logger.exception("error when running task: \n%s", inspect.getsource(task))
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("task finished in %sms: \n%s", int((time.time() - start) * 1000), inspect.getsource(task))
            self._total_tasks_processed += 1

    def _fail_channel_work(self, channel, exception):
        channel_future = channel.channel_future()
        if channel_future and not channel_future.done():
            channel_future.set_exception(exception)

        fail_pendings = getattr(channel, 'fail_pendings', None)
        if fail_pendings:
            fail_pendings(exception)

    def _close_channel_internally(self, channel, reason='', exception=None):
        assert self.in_eventloop(), "Must be in event loop"
        logger.debug(f"closing channel internally (reason: {reason}): {channel}")
        exception = exception or RuntimeError(f"channel closed: {reason or 'unknown reason'}")
        self._fail_channel_work(channel, exception)
        try:
            channel.socket().close()
        except Exception:
            logger.exception("error while closing channel socket: %s", channel)
        channel.close_future().set(channel)
        channel.set_active(False, reason)
        channel.unregister()
        self._channels.pop(channel.fileno0(), None)
        self._connect_timeout_due_millis.pop(channel.fileno0(), None)

    def _cleanup_resources(self, reason='event loop stopped'):
        if self._closed:
            return
        self._closed = True

        for channel in list(self._channels.values()):
            try:
                if self.in_eventloop():
                    self._close_channel_internally(channel, reason)
                else:
                    self._fail_channel_work(channel, RuntimeError(f"channel closed: {reason}"))
                    channel.socket().close()
                    channel.close_future().set(channel)
                    channel.set_active(False, reason)
                    fileno = channel.fileno0()
                    try:
                        self._selector.unregister(fileno)
                        channel.handler_context().fire_channel_unregistered()
                    except Exception as e:
                        logger.debug("channel %s/%s was not unregistered cleanly: %s", channel.id(), fileno, e)
                    self._channels.pop(fileno, None)
                    self._connect_timeout_due_millis.pop(fileno, None)
            except Exception:
                logger.exception("error while closing channel during event loop cleanup: %s", channel)

        try:
            self._selector.close()
        except Exception:
            logger.exception("error while closing selector")

        close_eventfd = getattr(self._eventfd, 'close', None)
        if close_eventfd:
            try:
                close_eventfd()
            except Exception:
                logger.exception("error while closing eventfd")

    def _events_to_str(self, events: List[Tuple[selectors.SelectorKey, int]]):
        result = []
        for key, flag in events:
            fileno = key.fd
            if fileno == self._eventfd.fileno():
                fd_name = f"EventFD({hex(id(self._eventfd))})"
            else:
                channel = self._channels.get(fileno)
                if not channel:
                    fd_name = f"unknown({fileno})"
                else:
                    fd_name = "%s(%s/%s)" % ('server' if channel.is_server() else 'client', fileno, channel.id())
            flags_str = flag_to_str(flag)
            result.append(f"{fd_name}:{flags_str}")
        return ", ".join(result)

    def _show_debug_info(self, n=50):
        # logger.debug(f'{"=" * n} {threading.current_thread().name} {"=" * n}')
        logger.debug(" counters ".center(n, '='))
        logger.debug("eventfd writes:        %s", self._eventfd_write_count)
        logger.debug("eventfd reads:         %s", self._eventfd_read_count)
        logger.debug("pending tasks:         %s", self._taskq.qsize())
        logger.debug("total sent bytes:      %s", self._total_sent)
        logger.debug("total received:        %s", self._total_received)
        logger.debug("total registered:      %s", self._total_registered)
        logger.debug("total accepted:        %s", self._total_accepted)
        logger.debug("total tasks submitted: %s", self._total_tasks_submitted)
        logger.debug("total tasks processed: %s", self._total_tasks_processed)
        logger.debug("pending connections:   %s", len(self._connect_timeout_due_millis))
        logger.debug("active connections:    %s", max(0, len(self._channels) - len(self._connect_timeout_due_millis)))
        logger.debug("current hooked:        %s", len(self._selector.get_map()))
        logger.debug("hooks:                 %s", self._selector._fd_to_key)

        logger.debug(" channels ".center(n, '='))
        for channel in self._channels.values():
            logger.debug(f"{channel}")

        logger.debug(" pendings ".center(n, '='))
        for channel in self._channels.values():
            if channel.is_server():  # server channel has no pendings
                continue
            channel_id = channel.id()
            if not channel.has_pendings():
                # logger.debug(f"{channel_id}: no pendings")
                continue
            chunk_count = 0
            bytes_count = 0
            for chunk in channel.pendings():
                chunk_count += 1
                bytes_count += len(chunk.buffer)
            logger.debug(f"{channel_id}: {chunk_count} chunks, {bytes_count} bytes in total")

    def _millis_to_wait_for_connect_timeout(self) -> int:  # in milliseconds
        if not self._connect_timeout_due_millis:
            return -1  # wait forever
        min_timeout = min(self._connect_timeout_due_millis.values())  # nearest timeout
        return max(0, min_timeout - int(time.time() * 1000))

    def _poll_timeout(self) -> int:
        millis_to_wait_for_connect_timeout = self._millis_to_wait_for_connect_timeout()
        if logger.isEnabledFor(logging.DEBUG):
            if millis_to_wait_for_connect_timeout < 0:
                timeout_millis = DEBUG_INTERVAL_MILLIS
            else:
                timeout_millis = min(DEBUG_INTERVAL_MILLIS, millis_to_wait_for_connect_timeout)
        else:
            timeout_millis = millis_to_wait_for_connect_timeout

        if timeout_millis < 0:
            return None
        return max(1, int(timeout_millis / 1000))

    def _poll(self) -> List[Tuple[selectors.SelectorKey, int]]:
        timeout = self._poll_timeout()

        if logger.isEnabledFor(logging.DEBUG):
            if timeout is None:
                logger.debug("poll timeout: infinity")
            else:
                logger.debug("poll timeout: %ss", timeout)

        events = self._selector.select(timeout)
        if not events and logger.isEnabledFor(logging.DEBUG):  # poll is interrupted by timeout
            self._show_debug_info()
        if events and logger.isEnabledFor(logging.DEBUG):
            logger.debug("events polled: %s", self._events_to_str(events))
        return events

    def _process_connection_timeout(self):
        if not self._connect_timeout_due_millis:
            return
        current = int(time.time() * 1000)
        if logger.isEnabledFor(logging.DEBUG):
            due_diff = {k: f"{max(0, v - current)}ms" for k, v in self._connect_timeout_due_millis.items()}
            logger.debug("checking connection timeout, countdowns: %s", due_diff)
        to_delete = []
        for fd, due_millis in dict(self._connect_timeout_due_millis).items():
            if due_millis <= current:
                channel = self._channels.get(fd)
                if channel and not channel._ever_active:
                    logger.error(f"connection timeout: {channel}")
                    self._close_channel_internally(channel, reason='connect timeout')
                to_delete.append(fd)
        for fd in to_delete:
            self._connect_timeout_due_millis.pop(fd, None)

    def _activate_channel(self, channel: AbstractChannel, reason: str):
        channel.set_active(True, reason)
        channel.channel_future().set(channel)
        self._connect_timeout_due_millis.pop(channel.fileno(), None)

    def _set_ssl_handshake_interest(self, channel: AbstractChannel, flag: int):
        if flag == selectors.EVENT_READ:
            channel.add_flag(selectors.EVENT_READ)
            channel.remove_flag(selectors.EVENT_WRITE)
        elif flag == selectors.EVENT_WRITE:
            channel.add_flag(selectors.EVENT_WRITE)

    def _complete_ssl_handshake(self, channel: AbstractChannel) -> bool:
        """Drive one step of a non-blocking client-side TLS handshake.

        ``SSLSocket.do_handshake()`` may complete immediately, or it may need
        the underlying socket to become readable or writable before it can make
        progress. ``SSLWantReadError`` and ``SSLWantWriteError`` are therefore
        readiness signals, not failures; the selector interest is adjusted and
        the caller must retry on the next matching event. Any other SSL/socket
        error is fatal for the connection, so the connect future receives the
        exception and the channel is closed.
        """
        try:
            channel.socket().do_handshake()
        except ssl.SSLWantReadError:
            # ``SSLWantReadError`` means the TLS handshake needs to read more data from the socket, but the socket is not readable yet. We need to wait for the socket to become readable before retrying the handshake. This is a normal part of the non-blocking TLS handshake process, so we adjust the selector interest to wait for readability and return False to indicate that the handshake is not complete yet.
            self._set_ssl_handshake_interest(channel, selectors.EVENT_READ)
            return False
        except ssl.SSLWantWriteError:
            self._set_ssl_handshake_interest(channel, selectors.EVENT_WRITE)
            return False
        except (ssl.SSLError, OSError) as e:
            channel.channel_future().set_exception(e)
            self._connect_timeout_due_millis.pop(channel.fileno0(), None)
            self._close_channel_internally(channel, reason=f'ssl handshake failed: {e}')
            return False

        channel.set_ssl_handshake_complete()
        channel.handler_context().fire_channel_handshake_complete()
        self._activate_channel(channel, 'ssl handshake complete')
        if channel.has_pendings():
            channel.add_flag(selectors.EVENT_WRITE)
        return True

    def _check_channel_active(self, channel: AbstractChannel):
        """Advance a connecting channel into the active state when possible.

        Unlike ``channel.is_active()``, this method is not a passive state
        check. It validates the completed non-blocking TCP connect with
        ``SO_ERROR``, drives any pending client-side TLS handshake, fires the
        relevant activation callbacks, and completes the connect future. It
        returns ``False`` when the channel is not ready for application I/O yet
        or when the connection failed; failures set the connect future exception
        and close the channel.
        """
        if channel._ever_active:
            return True

        try:
            connect_error = channel.socket().getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except OSError as e:
            channel.channel_future().set_exception(e)
            self._connect_timeout_due_millis.pop(channel.fileno0(), None)
            self._close_channel_internally(channel, reason=f'connect status check failed: {e}')
            return False

        if connect_error:
            exception = OSError(connect_error, os.strerror(connect_error))
            logger.error("connection failed: %s: %s", channel, exception)
            channel.channel_future().set_exception(exception)
            self._connect_timeout_due_millis.pop(channel.fileno0(), None)
            self._close_channel_internally(channel, reason=f'connect failed: {exception}')
            return False

        if channel.needs_ssl_handshake():
            return self._complete_ssl_handshake(channel)

        self._activate_channel(channel, 'first time to be active')
        return True

    @log(logger)
    def _start(self):
        self._thread = threading.current_thread()
        self._start_barrier.set()
        logger.debug(f"eventloop (EventFD:{hex(id(self._eventfd))}) started in thread: {self._thread.name}")
        while True:
            if self._stop_polling:
                self._cleanup_resources('event loop stopped')
                logger.debug(f"eventloop (EventFD:{hex(id(self._eventfd))}) closed in thread: {self._thread.name}")
                return

            for key, event in self._poll():
                fileno = key.fd
                if fileno == self._eventfd.fileno():  # just to wake up from epoll
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("EventFD %s interrupted", hex(id(self._eventfd)))
                    self._eventfd.unsafe_read()
                    self._eventfd_read_count += 1
                    continue

                channel = self._channels.get(fileno)
                if not channel:
                    logger.debug("channel not found by fileno: %s", fileno)
                    continue

                if channel.is_server():
                    server_channel = channel
                    if not server_channel.is_active():
                        server_channel.set_active(True, reason='server channel is always active')
                    for client_sock, client_addr in server_channel.acceptall():
                        self._total_accepted += 1
                        logger.debug("accepted: %s, address: %s", sockinfo(client_sock), client_addr)
                        server_channel.handler_context().fire_channel_read(client_sock)
                    continue

                if event & selectors.EVENT_WRITE:
                    if not self._check_channel_active(channel):
                        continue
                    if not channel.has_pendings():  # has no pending chunks
                        channel.remove_flag(selectors.EVENT_WRITE)
                    else:
                        chunks = channel.pendings()
                        while True:
                            head, *tail = chunks
                            if head.close:  # denote to close locally
                                logger.debug("process chunk with close indicator: %s", channel)
                                self._close_channel_internally(channel, 'chunk with close indicator')
                                break
                            l0 = len(head.buffer)
                            head.buffer = channel.try_send(head.buffer)
                            sent_bytes = l0 - len(head.buffer)
                            self._total_sent += sent_bytes
                            channel._pending_bytes -= sent_bytes
                            if not head.buffer:  # all data sent for this chunk
                                chunks = tail
                                head.future.set_result(True)
                                if not chunks:  # no chunks left
                                    break
                            else:   # still has data to send later for this chunk
                                break
                        channel.set_pendings(chunks)
                        if not channel.has_pendings():
                            channel.remove_flag(selectors.EVENT_WRITE)
                    channel._check_writability()

                if event & selectors.EVENT_READ and fileno in self._channels:
                    if not channel.is_active() and not self._check_channel_active(channel):
                        # if channel is not active yet, then check active
                        # if channel cannot be set to active, like TLS handshake waiting for read/write or connection failed, then skip processing application data for this event.
                        continue
                    buffer, eof = channel.recvall()
                    self._total_received += len(buffer)
                    if buffer:
                        # logger.info("receive: %s bytes: %s", len(buffer), buffer.decode('utf-8').replace('\n', '\\n'))
                        channel.handler_context().fire_channel_read(buffer)
                    elif eof:
                        self._close_channel_internally(channel, 'EOF')
                        continue

            self._process_task_queue()
            self._process_connection_timeout()

    def start(self):
        if self._stop_polling or self._closed:
            raise RuntimeError("event loop is stopping")
        if self._start_barrier.is_set():
            return
        with self._lock:
            if self._stop_polling or self._closed:
                raise RuntimeError("event loop is stopping")
            if self._start_barrier.is_set():
                return
            self._pool.submit(self._start)
            self._start_barrier.wait()


@define(slots=False)
class EventLoopGroup:

    num: int = field(default=1)                # 1 is enough for most cases, especially for high IO
    prefix: str = field(default="")  # prefix for eventloop name

    def __attrs_post_init__(self):
        self.pool = create_thread_pool(self.num, self.prefix)
        self.eventloops = [EventLoop(self.pool) for _ in range(self.pool._max_workers)]
        self._iter = itertools.cycle(self.eventloops)
        pass

    def get_eventloop(self) -> EventLoop:
        return self._iter.__next__()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for eventloop in self.eventloops:
            eventloop.stop()
        self.pool.shutdown()
