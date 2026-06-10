import selectors
import socket

from py_netty.utils import flag_to_str, sockinfo


class TestSockInfo:


    def test_sockinfo(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.settimeout(1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            listener_addr, listener_port = listener.getsockname()
            assert sockinfo(listener) == (
                f"[id: {hex(id(listener))}, fd: {listener.fileno()}, "
                f"L:/{listener_addr}:{listener_port}]"
            )

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(listener.getsockname())

                conn, _ = listener.accept()
                with conn:
                    client_addr, client_port = client.getsockname()
                    peer_addr, peer_port = client.getpeername()
                    assert sockinfo(client) == (
                        f"[id: {hex(id(client))}, fd: {client.fileno()}, "
                        f"L:/{client_addr}:{client_port} - "
                        f"R:/{peer_addr}:{peer_port}]"
                    )

                    conn_addr, conn_port = conn.getsockname()
                    conn_peer_addr, conn_peer_port = conn.getpeername()
                    assert sockinfo(conn) == (
                        f"[id: {hex(id(conn))}, fd: {conn.fileno()}, "
                        f"L:/{conn_addr}:{conn_port} - "
                        f"R:/{conn_peer_addr}:{conn_peer_port}]"
                    )

        closed_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed_sock.close()
        assert sockinfo(closed_sock) == str(closed_sock)


class TestFlagToStr:

    def test_flag_to_str(self):
        assert flag_to_str(0) == ""
        assert flag_to_str(selectors.EVENT_READ) == "R"
        assert flag_to_str(selectors.EVENT_WRITE) == "W"
        assert flag_to_str(selectors.EVENT_READ | selectors.EVENT_WRITE) == "R|W"
        assert flag_to_str(selectors.EVENT_READ | selectors.EVENT_WRITE | 4) == "R|W"
