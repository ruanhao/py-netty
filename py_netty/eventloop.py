import queue
import time
import itertools
import selectors
import logging
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

    def modify_flag(self, fileno, flag):
        self._selector.modify(fileno, flag)

    def submit_task(self, task):
        self.start()
        self._taskq.put(task)
        self._total_tasks_submitted += 1
        self.interrupt("submit task")

    def interrupt(self, desc=""):
        if desc and logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"interrupting eventloop with EventFD {hex(id(self._eventfd))} in {self._thread.name}: {desc}")
        self._eventfd.unsafe_write()

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
        except Exception:
            pass
        self._channels.pop(fileno, None)
        self._connect_timeout_due_millis.pop(fileno, None)
        cf.set(channel)
        return cf

    def in_eventloop(self):
        return self._thread == threading.current_thread()

    def register(self, channel: AbstractChannel) -> ChannelFuture:
        self.start()

        if not self.in_eventloop():
            self.submit_task(lambda: self.register(channel))
            return channel.channel_future()

        channel.socket().setblocking(False)

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
        self._stop_polling = True
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

    def _close_channel_internally(self, channel, reason=''):
        assert self.in_eventloop(), "Must be in event loop"
        logger.debug(f"closing channel internally (reason: {reason}): {channel}")
        channel.socket().close()
        channel.close_future().set(channel)
        channel.set_active(False, reason)
        channel.unregister()

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

    def _check_channel_active(self, channel: AbstractChannel):
        if channel._ever_active:
            return
        try:
            channel.socket().getpeername()
            no_peer = False
        except Exception:
            no_peer = True
        if no_peer:
            if not channel._ever_active:
                channel.set_active(False, 'no peer')
                channel.channel_future().set(channel)
            else:               # half close
                pass
            return
        if not channel._ever_active:  # first time to be active
            channel.set_active(True, 'first time to be active')
            channel.channel_future().set(channel)
            self._connect_timeout_due_millis.pop(channel.fileno(), None)

    @log(logger)
    def _start(self):
        self._thread = threading.current_thread()
        self._start_barrier.set()
        logger.debug(f"eventloop (EventFD:{hex(id(self._eventfd))}) started in thread: {self._thread.name}")
        while True:
            if self._stop_polling:
                self._selector.close()
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
                    self._check_channel_active(channel)
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
                            self._total_sent += (l0 - len(head.buffer))
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

                if event & selectors.EVENT_READ and fileno in self._channels:
                    buffer, eof = channel.recvall()
                    self._total_received += len(buffer)
                    if buffer:
                        self._check_channel_active(channel)
                        # logger.info("receive: %s bytes: %s", len(buffer), buffer.decode('utf-8').replace('\n', '\\n'))
                        channel.handler_context().fire_channel_read(buffer)
                    elif eof:
                        self._close_channel_internally(channel, 'EOF')
                        continue

            self._process_task_queue()
            self._process_connection_timeout()

    def start(self):
        if self._start_barrier.is_set():
            return
        with self._lock:
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
