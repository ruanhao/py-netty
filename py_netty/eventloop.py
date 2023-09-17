import queue
import itertools
import select
import logging
import threading
from .eventfd import eventfd
from concurrent.futures import ThreadPoolExecutor
from .utils import create_thread_pool, sockinfo, log, LoggerAdapter
from .channel import ChannelFuture, AbstractChannel
from dataclasses import dataclass


logger = LoggerAdapter(logging.getLogger(__name__))


class EventLoop:

    def __init__(self, pool: ThreadPoolExecutor):
        assert pool, "thread pool executor is required"

        # internals
        self._channels = {}  # {fileno: Channel}
        self._thread = None
        self._stop_polling = False
        self._start_barrier = threading.Event()
        self._lock = threading.Lock()
        self._pool = pool

        # poll object
        self._eventfd = eventfd()
        self._epoll = self._get_poll_obj()
        self._epoll.register(self._eventfd, select.POLLIN)

        # queues
        self._taskq = queue.Queue()

        # counters
        self._total_accepted = 0
        self._total_sent = 0
        self._total_received = 0
        self._total_registered = 0
        self._total_tasks_submitted = 0
        self._total_tasks_processed = 0

    def _get_poll_obj(self):
        try:
            self._linux = True
            return select.epoll()
        except Exception:
            self._linux = False
            return select.poll()

    def submit_task(self, task):
        self.start()
        self._taskq.put(task)
        self._total_tasks_submitted += 1
        self.interrupt("submit task")

    def interrupt(self, desc=""):
        if desc:
            logger.debug(f"Interrupting eventloop (EventFD:{hex(id(self._eventfd))}) in {self._thread.name}: {desc}")
        self._eventfd.unsafe_write()

    def unregister(self, channel: AbstractChannel):
        fileno = channel.fileno0()
        try:
            self._epoll.unregister(fileno)
            logger.debug(f"Unregistered fd({fileno}) from epoll")
        except Exception:
            pass
        self._channels.pop(fileno, None)

    def in_eventloop(self):
        return self._thread == threading.current_thread()

    def register(self, channel: AbstractChannel, channel_future: ChannelFuture = None) -> ChannelFuture:
        self.start()

        if not self.in_eventloop():
            cf = ChannelFuture()
            self.submit_task(lambda: self.register(channel, cf))
            return cf

        sock = channel.socket()
        sock.setblocking(0)

        flag = select.POLLIN | select.POLLHUP | select.POLLOUT
        # not sure if this is a bug, but on linux, if I register a server socket with EPOLLET,
        # when there is a flood of connections, epoll will not trigger POLLIN event for the server socket to accept,
        # even if there are connections waiting to be accepted.
        # if self._linux and not is_server:
        #     flag |= select.EPOLLET
        channel.set_flag(flag)
        self._epoll.register(channel, flag)
        self._total_registered += 1
        self._channels[channel.fileno()] = channel
        logger.debug(f"Registered {sockinfo(sock)} with flag: {flag}")
        cf = channel_future or ChannelFuture()
        cf.set(channel)
        return cf

    def stop(self):
        logger.debug("stopping epoll")
        self._stop_polling = True
        self.interrupt('stop epoll')

    def _process_task_queue(self):
        while not self._taskq.empty():
            task = self._taskq.get()
            logger.debug(f"Runing task: {task}")
            try:
                task()
            except Exception:
                logger.exception(f"Error running task: {task}")
            logger.debug(f"Task done: {task}")
            self._total_tasks_processed += 1

    def _close_channel_internally(self, channel, reason=''):
        assert self.in_eventloop(), "Must be in event loop"
        logger.debug(f"closing channel(active:{channel.is_active()}) internally: {channel.fileno0()}, reason: {reason}")
        channel.socket().close()
        channel.close_future().set(channel)
        channel.set_active(False)
        channel.unregister()

    def _events_to_str(self, events):
        result = []
        for fileno, flag in events:
            channel = self._channels.get(fileno)
            if not channel:
                continue
            flags = []
            if fileno == self._eventfd.fileno():
                fdname = f"EventFD({hex(id(self._eventfd))})"
            elif channel.is_server():
                fdname = f"server({fileno})"
            else:
                fdname = f"client({fileno})"

            if flag & select.POLLIN:
                flags.append("POLLIN")
            if flag & select.POLLOUT:
                flags.append("POLLOUT")
            if flag & select.POLLHUP:
                flags.append("POLLHUP")
            flags_str = "|".join(flags)
            result.append(f"{fdname}:{flags_str}")
        return ", ".join(result)

    def _show_debug_info(self, n=25):
        logger.debug(f'{"=" * n} {threading.current_thread().name} {"=" * n}')

        logger.debug("[Counter] TaskQ: %s", self._taskq.qsize())
        logger.debug("[Counter] Total sent: %s", self._total_sent)
        logger.debug("[Counter] Total received: %s", self._total_received)
        logger.debug("[Counter] Total registered: %s", self._total_registered)
        logger.debug("[Counter] Total accepted: %s", self._total_accepted)
        logger.debug("[Counter] Total tasks submitted: %s", self._total_tasks_submitted)
        logger.debug("[Counter] Total tasks processed: %s", self._total_tasks_processed)
        logger.debug("[Counter] Total current conns: %s", len(self._channels))

    @log(logger)
    def _start(self):
        self._thread = threading.current_thread()
        self._start_barrier.set()
        logger.debug(f"EventLoop (EventFD:{hex(id(self._eventfd))}) started in thread: {self._thread.name}")
        while True:
            if self._stop_polling:
                self._epoll.close()
                return

            # events = self._epoll.poll(10 if logger.isEnabledFor(logging.DEBUG) else None)
            if logger.isEnabledFor(logging.DEBUG):
                seconds = 10
                events = self._epoll.poll(seconds * (1 if self._linux else 1000))
            else:
                events = self._epoll.poll()

            if not events and logger.isEnabledFor(logging.DEBUG):
                self._show_debug_info()
            if events:
                logger.debug("Events: %s", self._events_to_str(events))

            for fileno, event in events:
                if fileno == self._eventfd.fileno():  # just to wake up from epoll
                    logger.debug("Interrupted")
                    self._eventfd.unsafe_read()
                    continue

                channel = self._channels.get(fileno)
                if not channel:
                    logger.error("Channel not found: %s", fileno)
                    continue

                if channel.is_server():
                    server_channel = channel
                    if not server_channel.is_active():
                        server_channel.set_active(True)
                        server_channel.handler().channel_active(server_channel.context())
                    for client_sock, client_addr in server_channel.acceptall():
                        self._total_accepted += 1
                        logger.debug("Accepted: %s, address: %s, total: %s", sockinfo(client_sock), client_addr, self._total_accepted)
                        server_channel.handler().initialize_child(server_channel.context(), client_sock)
                    continue

                if event & select.POLLOUT:
                    if not channel.has_pendings():  # no pending chunks
                        channel.set_active(True)
                        channel.remove_flag(select.POLLOUT)
                        continue
                    chunks = channel.pendings()
                    while True:
                        head, *tail = chunks
                        if head.close:  # denote to close locally
                            logger.debug("Hit Chunk with close indicator: %s", sockinfo(channel.socket()))
                            self._close_channel_internally(channel, 'hit chunk with close indicator')
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
                        channel.remove_flag(select.POLLOUT)

                if event & select.POLLIN and fileno in self._channels:
                    buffer = channel.recvall()
                    self._total_received += len(buffer)
                    if buffer:
                        # logger.info("receive: %s bytes: %s", len(buffer), buffer.decode('utf-8').replace('\n', '\\n'))
                        channel.handler().channel_read(channel.context(), buffer)
                    else:       # EOF
                        self._close_channel_internally(channel, 'EOF')
                        continue

                if event & select.POLLHUP:
                    if not event & select.POLLIN:
                        self._close_channel_internally(channel, 'HUP')

            self._process_task_queue()

    def start(self):
        if self._start_barrier.is_set():
            return
        with self._lock:
            if self._start_barrier.is_set():
                return
            self._pool.submit(self._start)
            self._start_barrier.wait()


@dataclass
class EventLoopGroup:

    num: int = None
    prefix: str = ""

    def __post_init__(self):
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
