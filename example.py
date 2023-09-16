from py_netty import ServerBootstrap, Bootstrap
from py_netty.handler import NoOpChannelHandler
import time
import argparse
import logging


class HttpHandler(NoOpChannelHandler):

    def channel_read(self, ctx, buffer):
        print(buffer.decode('utf-8'))


def http_client(remote_address, remote_port):
    b = Bootstrap(handler=HttpHandler())
    channel = b.connect(remote_address, remote_port).sync()
    request = f'GET / HTTP/1.1\r\nHost: {remote_address}\r\n\r\n'
    channel.write(request.encode('utf-8'))


def run_echo_server(local_port, local_address='0.0.0.0'):
    ServerBootstrap().bind(address=local_address, port=local_port)


def main():

    parser = argparse.ArgumentParser(description='Py-Netty example')
    parser.add_argument('-s', dest='as_server', action='store_true', help='run as server')
    parser.add_argument('-lp', dest='local_port', type=int, default=8080, help='local port for server to bind')
    parser.add_argument('-la', dest='local_address', type=str, default='localhost', help='local address for server to bind')
    parser.add_argument('-c', dest='as_client', action='store_true', help='run as client')
    parser.add_argument('-rp', dest='remote_port', type=int, default=80, help='remote port for client')
    parser.add_argument('-ra', dest='remote_address', type=str, default='www.google.com', help='remote address for client')
    parser.add_argument('-v', dest='verbose', action='store_true', help='verbose mode')
    args = parser.parse_args()

    logging.basicConfig(
        handlers=[logging.StreamHandler()],
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(threadName)s - %(message)s',
    )

    if args.as_server:
        run_echo_server(args.local_port, args.local_address)
    elif args.as_client:
        http_client(args.remote_address, args.remote_port)

    print('Press Ctrl-C to exit...')
    time.sleep(65802)


if __name__ == '__main__':
    main()
