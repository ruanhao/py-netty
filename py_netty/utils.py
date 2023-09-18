import sys
import select
import socket
import logging
import itertools
import traceback
from functools import wraps
from concurrent import futures


_counter = itertools.count()


# TODO add a context manager to recover the origin blocking mode
def acceptall(sock: socket.socket) -> list:
    result = []
    origin_blocking = sock.getblocking()
    if origin_blocking:
        sock.setblocking(0)
    try:
        while True:
            try:
                result.append(sock.accept())
            except socket.error:
                return result
    finally:
        if origin_blocking:
            sock.setblocking(origin_blocking)


def recvall(sock, timeout=0):
    """if timeout is non-zero, it will block at the first time"""
    buffer = b''
    origin_blocking = sock.getblocking()
    if origin_blocking:
        sock.setblocking(0)
    try:
        while True:
            if select.select([sock], [], [], timeout):
                try:
                    received = sock.recv(1024)
                    if not received:  # EOF
                        return buffer
                    buffer += received
                    timeout = 0
                except socket.error:
                    return buffer
            else:               # timeout
                return buffer
    finally:
        if origin_blocking:
            sock.setblocking(origin_blocking)


def sendall(sock, buffer, spin=2) -> bytes:
    origin_blocking = sock.getblocking()
    if origin_blocking:
        sock.setblocking(0)
    total_sent = 0
    try:
        while total_sent < len(buffer):
            try:
                total_sent += sock.send(buffer[total_sent:])
            except socket.error:
                if spin > 0:
                    spin -= 1
                    continue
                break
        return buffer[total_sent:]
    finally:
        if origin_blocking:
            sock.setblocking(origin_blocking)


def sockinfo(sock):
    '''Exampble: [id: 0xd829bade, L:/127.0.0.1:2069 - R:/127.0.0.1:55666]'''
    sock_id = hex(id(sock))
    fileno = sock.fileno()
    s_addr = None
    try:
        s_addr, s_port = sock.getsockname()
        d_addr, d_port = sock.getpeername()
        return f"[id: {sock_id}, fd: {fileno}, L:/{s_addr}:{s_port} - R:/{d_addr}:{d_port}]"
    except Exception:
        if s_addr:
            return f"[id: {sock_id}, fd: {fileno}, L:/{s_addr}:{s_port}]"
        else:
            # return f"[id: {sock_id}, fd: {fileno}, CLOSED]"
            return str(sock)


def create_thread_pool(n=None, prefix=''):
    return futures.ThreadPoolExecutor(max_workers=n, thread_name_prefix=prefix or f'EventloopGroup-{next(_counter)}')


def log(logger: logging.Logger = None, console: bool = True):

    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kw):
            try:
                return func(*args, **kw)
            except Exception as e:
                if logger:
                    logger.exception('unhandled exception: %s', str(e))
                if console:
                    print(traceback.format_exc(), file=sys.stderr)
        return wrapper
    return decorate


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger, prefix='py-netty'):
        super(LoggerAdapter, self).__init__(logger, {})
        self.prefix = prefix

    def process(self, msg, kwargs):
        if self.prefix:
            return '[%s] %s' % (self.prefix, msg), kwargs
        else:
            return msg, kwargs
