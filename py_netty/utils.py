import sys
import logging
import itertools
import traceback
from functools import wraps
from concurrent import futures
import selectors

_counter = itertools.count()


def _format_sockaddr(sockaddr):
    address, port = sockaddr[:2]
    if isinstance(address, str) and ':' in address:
        return f"[{address}]:{port}"
    return f"{address}:{port}"


def sockinfo(sock):
    """Example: [id: 0xd829bade, L:/127.0.0.1:2069 - R:/127.0.0.1:55666]"""
    sock_id = hex(id(sock))
    fileno = sock.fileno()
    s_addr = None
    try:
        sockname = sock.getsockname()
        s_addr = sockname[:2]
        peername = sock.getpeername()
        return f"[id: {sock_id}, fd: {fileno}, L:/{_format_sockaddr(sockname)} - R:/{_format_sockaddr(peername)}]"
    except Exception:
        if s_addr:
            return f"[id: {sock_id}, fd: {fileno}, L:/{_format_sockaddr(s_addr)}]"
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


def flag_to_str(flag):
    flags = []
    if flag & selectors.EVENT_READ:
        flags.append("R")
    if flag & selectors.EVENT_WRITE:
        flags.append("W")
    return "|".join(flags)
