#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import click
from collections import defaultdict
from attrs import define, field
import time
import threading
from datetime import datetime
import asyncio
import socket
from py_netty import ServerBootstrap, EventLoopGroup
from py_netty.handler import EchoChannelHandler


class MyEchoChannelHandler(EchoChannelHandler):

    def exception_caught(self, ctx, exception: Exception) -> None:
        print(f"[Exception Caught] {ctx.channel()} : {str(exception)}")

    def channel_inactive(self, ctx):
        thread_name = threading.current_thread().name
        print(f"[{thread_name}] Connection closed:", self.raddr)
        del clients[self.raddr]

    def channel_active(self, ctx):
        super().channel_active(ctx)
        raddr = ctx.channel().socket().getpeername()
        _ = clients[raddr]
        thread_name = threading.current_thread().name
        print(f"[{thread_name}] Connection established:", raddr)
        self.raddr = raddr

    def channel_read(self, ctx, msg):
        clients[self.raddr].read(len(msg))
        ctx.write(msg)


def submit_daemon_thread(func, *args, **kwargs) -> threading.Thread:

    def _worker():
        func(*args, **kwargs)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def format_bytes(size, scale=1):
    # 2**10 = 1024
    size = int(size)
    power = 2**10
    n = 0
    power_labels = {0 : '', 1: 'k', 2: 'm', 3: 'g', 4: 't'}
    while size > power:
        size /= power
        size = round(size, scale)
        n += 1
    return size, power_labels[n]


def pretty_duration(seconds: int) -> str:
    TIME_DURATION_UNITS = (
        ('W', 60 * 60 * 24 * 7),
        ('D', 60 * 60 * 24),
        ('H', 60 * 60),
        ('M', 60),
        ('S', 1)
    )
    if seconds == 0:
        return '0S'
    parts = []
    for unit, div in TIME_DURATION_UNITS:
        amount, seconds = divmod(int(seconds), div)
        if amount > 0:
            parts.append('{}{}'.format(amount, unit))
    return ', '.join(parts)


@define(slots=True, kw_only=True, order=True)
class _Client():
    last_read_time: float = field(factory=time.time)
    total_read_bytes: int = field(default=0)
    cumulative_read_bytes: int = field(default=0)  # bytes
    cumulative_read_time: float = field(default=0.0)  # seconds
    rbps: float = field(default=0.0)
    born_time: float = field(factory=time.time)

    def pretty_born_time(self):
        return pretty_duration(time.time() - self.born_time)

    def pretty_speed(self):
        v, unit = format_bytes(self.rbps)
        return f"{v:.2f} {unit}/s"

    def pretty_total(self):
        v, unit = format_bytes(self.total_read_bytes)
        if unit:
            return f"{v:.2f} {unit}"
        else:
            return f"{v} B"

    def read(self, size):
        current_time = time.time()
        self.cumulative_read_time += (current_time - self.last_read_time)
        self.last_read_time = current_time
        self.total_read_bytes += size
        self.cumulative_read_bytes += size
        if self.cumulative_read_time > 1:
            self.rbps = int(self.cumulative_read_bytes / self.cumulative_read_time)  # bytes per second
            self.cumulative_read_time = 0
            self.cumulative_read_bytes = 0

    def check(self):
        if time.time() - self.last_read_time > 2:
            self.rbps = 0
            self.cumulative_read_time = 0
            self.cumulative_read_bytes = 0
        pass


clients = defaultdict(_Client)


def clients_check(interval=3):
    ever = False
    zzz = 0
    rounds = 0
    while True:
        # if clients:
        #     cls()
        clients_snapshot = clients.copy()
        items = list(clients_snapshot.items())
        items.sort(key=lambda x: x[1].born_time)
        total = len(clients_snapshot)
        if total:
            rounds += 1
            rampup_seconds = round(items[-1][1].born_time - items[0][1].born_time, 2)
            print(f'{datetime.now()} (total:{total}, rounds:{rounds}, rampup:{rampup_seconds}s)'.center(100, '-'))
            ever = True
            zzz = 0
        else:
            if zzz % 60 == 0 and ever:
                rounds += 1
                print(f"{datetime.now()} No client connected (rounds:{rounds})".center(100, '-'))
            zzz += 1

        count = 1
        for address, client in items:
            client.check()
            ip, port = address
            ipport = f"{ip}:{port}"
            pspeed = client.pretty_speed()
            ptotal = client.pretty_total()
            duration = client.pretty_born_time().lower()
            print(f"[{count:3}] | {ipport:21} | speed: {pspeed:15} | total read: {ptotal:10} | duration: {duration}")
            count += 1
            # print("-" * 50)
        if total:
            average_speed = round(sum([c.rbps for c in clients_snapshot.values()]) / total, 2)
            print(f"{'Average Speed:':<50} {average_speed} bytes/s")

        time.sleep(interval)


@click.group()
def cli():
    pass


@cli.command(help='Echo server')
@click.option('--port', '-p', type=int, default=8080, help='Port')
@click.option('--nio', is_flag=True, help='Using py-netty')
@click.option('--asyncio', 'using_asyncio', is_flag=True, help='Using asnycio')
def run_echo_server(port, nio, using_asyncio):
    """
    simple version: socat TCP4-LISTEN:2000,fork EXEC:cat
    """

    if nio and using_asyncio:
        print("--nio and --asyncio cannot be used together")
        return

    submit_daemon_thread(clients_check)

    if using_asyncio:
        async def _handle_client(reader, writer):
            address = writer.get_extra_info('peername')
            print(f"Connection established: {address}")
            _ = clients[address]

            try:
                while True:
                    buffer = await reader.read(8192)
                    client = clients[address]
                    client.read(len(buffer))

                    if not buffer:
                        break

                    writer.write(buffer)
                    await writer.drain()
            finally:
                print(f"Connection closed: {address}")
                del clients[address]
                writer.close()

        async def _run_server():
            server = await asyncio.start_server(_handle_client, '0.0.0.0', port, reuse_address=True)
            async with server:
                await server.serve_forever()

        print(f"Async proxy server started listening: (0.0.0.0:{port}) ...")
        asyncio.run(_run_server())
        return

    if nio:
        print(f"NIO proxy server started listening: (0.0.0.0:{port}) ...")
        ServerBootstrap(child_handler_initializer=MyEchoChannelHandler, child_group=EventLoopGroup(1, 'nio-client'))\
            .bind(address='0.0.0.0', port=port)\
            .close_future()\
            .sync()
        return

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(('0.0.0.0', port))
    server_socket.listen()
    print(f"Proxy server started listening: (0.0.0.0:{port}) ...")

    def __handle(socket, address, count=0):
        thread_name = threading.current_thread().name
        print(f"[{thread_name}] Connection established:", address)
        _ = clients[address]
        try:
            while True:
                buffer = socket.recv(1024 * 8)
                client = clients[address]
                client.read(len(buffer))

                if not buffer:
                    break

                socket.sendall(buffer)
        finally:
            print(f"[{thread_name}] Connection closed:", address)
            del clients[address]
            socket.close()

    while True:
        client_socket, address = server_socket.accept()
        print("Connection accepted:", address)
        submit_daemon_thread(__handle, client_socket, address)


def main():
    cli()


if __name__ == '__main__':
    main()
