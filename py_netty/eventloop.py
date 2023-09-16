import queue
import itertools
import select
import socket
import logging
import threading
from .bytebuf import Chunk
from .eventfd import eventfd
from concurrent.futures import Future, ThreadPoolExecutor
from .utils import create_thread_pool, sockinfo, sendall, recvall, acceptall, log, LoggerAdapter
from .channel import AbstractChannel, NioSocketChannel, NioServerSocketChannel, ChannelFuture, ChannelContext
from .handler import AbstractChannelHandler, NoOpChannelHandler
from dataclasses import dataclass


logger = LoggerAdapter(logging.getLogger(__name__))


class EventLoop:

    def __init__(self, pool: ThreadPoolExecutor):
        assert pool, "thread pool executor is required"

        self._thread = None
        self._stop_polling = False
        self._start_barrier = threading.Event()
        self._lock = threading.Lock()
        self._pool = pool

        # poll object
        self._eventfd = eventfd()
        self._epoll = self._get_poll_obj()
        self._epoll.register(self._eventfd, select.POLLIN)

        # internals
        self._flags = {}          # {fileno: flag}
        self._socks = {}          # {fileno: socket}
        self._pendings = {}       # {'file_no': [Chunk, Chunk, ...]}
        self._handlers = {}       # {fileno: handler}
        self._server_socket = {}  # {fileno: is_server_listening_socket}
        self._contexts = {}       # {fileno: ChannelContext}

        # queues
        self._writeq = queue.Queue()
        self._closeq = queue.Queue()  # locally closed
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
        self.interrupt()

    def close_on_complete(self, fileno) -> Future:
        c = Chunk(b'', close=True)
        self._writeq.put((fileno, c))
        self.interrupt()
        return c.future

    def close_forcibly(self, fileno, future: Future = None):
        self._closeq.put((fileno, future))
        self.interrupt()

    def interrupt(self):
        self._eventfd.unsafe_write()

    def unregister(self, fileno):
        sock = self._socks[fileno]
        logger.debug(f"Unregistering {sockinfo(sock)}")
        try:
            self._epoll.unregister(fileno)
            logger.debug(f"Unregistered fd({fileno}) from epoll")
        except Exception:
            pass
        self._socks[fileno].close()

        self._socks.pop(fileno, None)
        self._flags.pop(fileno, None)
        self._server_socket.pop(fileno, None)
        self._handlers.pop(fileno, None)
        self._contexts.pop(fileno, None)
        self._pendings.pop(fileno, None)

    def in_event_loop(self):
        return self._thread == threading.current_thread()

    def register(
            self,
            connection: socket.socket,
            is_server: bool = False,
            handler: AbstractChannelHandler = None,
            channel_future: ChannelFuture = None
    ) -> AbstractChannel:
        self.start()            # make sure the loop is started
        if not self.in_event_loop():
            cf = ChannelFuture()
            self.submit_task(lambda: self.register(connection, is_server, handler, cf))
            return cf
        connection.setblocking(0)
        flag = select.POLLIN | select.POLLHUP
        # not sure if this is a bug, but on linux, if I register a server socket with EPOLLET,
        # when there is a flood of connections, epoll will not trigger POLLIN event for the server socket to accept,
        # even if there are connections waiting to be accepted.
        # if self._linux and not is_server:
        #     flag |= select.EPOLLET
        self._flags[connection.fileno()] = flag
        self._epoll.register(connection.fileno(), flag)
        self._total_registered += 1
        logger.debug(f"Registered {sockinfo(connection)}, flag: {flag}")
        self._socks[connection.fileno()] = connection
        self._server_socket[connection.fileno()] = is_server
        self._handlers[connection.fileno()] = handler or NoOpChannelHandler()

        if not is_server:
            channel = NioSocketChannel(self, connection)
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        else:
            channel = NioServerSocketChannel(self, connection)

        context = ChannelContext(channel)
        self._contexts[connection.fileno()] = context
        self._handlers[connection.fileno()].channel_active(context)
        cf = channel_future or ChannelFuture()
        cf.set(channel)
        return cf

    def stop(self):
        logger.debug("stopping epoll")
        self._stop_polling = True
        self.interrupt()

    def add_flag(self, fileno, flag):
        if self._flags[fileno] & flag:
            return
        self._flags[fileno] |= flag
        self._epoll.modify(fileno, self._flags[fileno])

    def remove_flag(self, fileno, flag):
        if fileno not in self._flags or not self._flags[fileno] & flag:
            return
        self._flags[fileno] &= ~flag
        self._epoll.modify(fileno, self._flags[fileno])

    def add_pending(self, fileno, chunk: Chunk):
        if not chunk.close and not chunk.buffer:
            chunk.future.set_result(True)
            return
        if fileno not in self._pendings:
            self._pendings[fileno] = [chunk]
        else:
            self._pendings[fileno].append(chunk)
        self.add_flag(fileno, select.POLLOUT)

    def write(self, fileno, buffer) -> Future:
        if not self.in_event_loop():
            assert self._thread is not None, "EventLoop not started yet"
            chunk = Chunk(buffer)
            self._writeq.put((fileno, chunk))
            self.interrupt()
            return chunk.future

        if not buffer:
            f = Future()
            f.set_result(True)
            return f

        if self._pendings.get(fileno):  # already has pending
            chunk = Chunk(buffer)
        else:
            pending = sendall(self._socks[fileno], buffer)
            self._total_sent += (len(buffer) - len(pending))
            chunk = Chunk(pending)

        self.add_pending(fileno, chunk)
        return chunk.future

    def _process_write_queue(self):
        while not self._writeq.empty():
            fileno, chunk = self._writeq.get()
            self.add_pending(fileno, chunk)

    def _process_task_queue(self):
        while not self._taskq.empty():
            task = self._taskq.get()
            # logger.debug(f"Runing task: {task}")
            task()
            self._total_tasks_processed += 1

    def _process_close_queue(self):
        while not self._closeq.empty():
            fileno, future = self._closeq.get()
            sock = self._socks[fileno]
            logger.debug(f"Closing {sockinfo(sock)}")
            sock.close()
            if future:
                future.set_result(True)
            self._handlers[fileno].channel_inactive(self._contexts[fileno])
            self.unregister(fileno)

    def _events_to_str(self, events):
        result = []
        for fileno, flag in events:
            flags = []
            if fileno == self._eventfd.fileno():
                fdname = "eventfd"
            elif self._server_socket.get(fileno, False):
                fdname = f"server({fileno})"
            elif fileno in self._socks:
                # fdname = sockinfo(self._socks[fileno])
                fdname = f"client({fileno})"
            else:
                fdname = "unknown"

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

        logger.debug("[INTERNALS] Connections: %s", self._socks)
        logger.debug("[INTERNALS] ServerSockets: %s", self._server_socket)
        logger.debug("[INTERNALS] Handlers: %s", self._handlers)
        logger.debug("[INTERNALS] Contexts: %s", self._contexts)
        logger.debug("[INTERNALS] Flags: %s", self._flags)
        logger.debug("[INTERNALS] Pendings: %s", self._pendings)

        logger.debug("[Counter] WriteQ: %s", self._writeq.qsize())
        logger.debug("[Counter] TaskQ: %s", self._taskq.qsize())
        logger.debug("[Counter] CloseQ: %s", self._closeq.qsize())
        logger.debug("[Counter] Total sent: %s", self._total_sent)
        logger.debug("[Counter] Total received: %s", self._total_received)
        logger.debug("[Counter] Total registered: %s", self._total_registered)
        logger.debug("[Counter] Total accepted: %s", self._total_accepted)
        logger.debug("[Counter] Total tasks submitted: %s", self._total_tasks_submitted)
        logger.debug("[Counter] Total tasks processed: %s", self._total_tasks_processed)
        logger.debug("[Counter] Total current conns: %s", len(self._socks))

    @log(logger)
    def _start(self):
        self._thread = threading.current_thread()
        self._start_barrier.set()
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

            self._process_close_queue()
            self._process_write_queue()
            self._process_task_queue()

            for fileno, event in events:
                if fileno == self._eventfd.fileno():  # just to wake up from epoll
                    # logger.debug("Interrupted")
                    self._eventfd.unsafe_read()
                    continue

                if self._server_socket.get(fileno):
                    for connection, address in acceptall(self._socks[fileno]):
                        self._total_accepted += 1
                        # print("Total accepted: %s" % self._accepted)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("Accepted: %s, address: %s, total: %s", sockinfo(connection), address, self._total_accepted)
                        self._handlers[fileno].channel_read(self._contexts[fileno], connection)
                    continue

                if event & select.POLLOUT:
                    chunks = self._pendings.get(fileno, [])
                    if not chunks:  # no pending chunks
                        self.remove_flag(fileno, select.POLLOUT)
                        continue

                    while True:
                        head, *tail = chunks
                        if head.close:  # denote to close locally
                            logger.debug("Close ON_COMPLETE: %s", sockinfo(self._socks[fileno]))
                            self.close_forcibly(fileno, head.future)
                            break
                        l0 = len(head.buffer)
                        head.buffer = sendall(self._socks[fileno], head.buffer)
                        self._total_sent += (l0 - len(head.buffer))
                        if not head.buffer:  # all data sent for this chunk
                            chunks = tail
                            head.future.set_result(True)
                            if not chunks:  # no chunks left
                                break
                        else:   # still has data to send later for this chunk
                            break
                    self._pendings[fileno] = chunks
                    if not chunks:
                        self.remove_flag(fileno, select.POLLOUT)

                if event & select.POLLIN:
                    buffer = recvall(self._socks[fileno])
                    self._total_received += len(buffer)
                    if buffer:
                        self._handlers[fileno].channel_read(self._contexts[fileno], buffer)
                        # logger.info("receive: %s bytes: %s", len(buffer), buffer.decode('utf-8').replace('\n', '\\n'))
                    else:       # EOF
                        self._handlers[fileno].channel_inactive(self._contexts[fileno])
                        self.unregister(fileno)
                        continue
                if event & select.POLLHUP:
                    if not event & select.POLLIN:
                        self._handlers[fileno].channel_inactive(self._contexts[fileno])
                        self.unregister(fileno)

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
