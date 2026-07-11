import logging
import selectors
import socket

from py_netty.utils import LoggerAdapter, create_thread_pool, flag_to_str, log, sockinfo


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

    def test_sockinfo_formats_ipv6_sockaddr(self):
        class FakeIpv6Socket:
            def fileno(self):
                return 100

            def getsockname(self):
                return ("::1", 12345, 0, 0)

            def getpeername(self):
                return ("2001:db8::1", 443, 0, 0)

        sock = FakeIpv6Socket()

        assert sockinfo(sock) == (
            f"[id: {hex(id(sock))}, fd: 100, "
            "L:/[::1]:12345 - R:/[2001:db8::1]:443]"
        )


class TestFlagToStr:

    def test_flag_to_str(self):
        assert flag_to_str(0) == ""
        assert flag_to_str(selectors.EVENT_READ) == "R"
        assert flag_to_str(selectors.EVENT_WRITE) == "W"
        assert flag_to_str(selectors.EVENT_READ | selectors.EVENT_WRITE) == "R|W"
        assert flag_to_str(selectors.EVENT_READ | selectors.EVENT_WRITE | 4) == "R|W"


class TestLogDecorator:

    def test_returns_wrapped_function_result(self):
        @log(console=False)
        def ok(value):
            return value + 1

        assert ok(1) == 2

    def test_logs_and_prints_traceback_when_wrapped_function_raises(self, caplog, capsys):
        logger = LoggerAdapter(logging.getLogger("tests.utils"))

        @log(logger=logger, console=True)
        def fail():
            raise RuntimeError("boom")

        result = fail()

        captured = capsys.readouterr()
        assert result is None
        assert "RuntimeError: boom" in captured.err
        assert "unhandled exception: boom" in caplog.text

    def test_suppresses_console_output_when_disabled(self, capsys):
        @log(console=False)
        def fail():
            raise RuntimeError("hidden")

        assert fail() is None
        captured = capsys.readouterr()
        assert captured.err == ""


class TestLoggerAdapter:

    def test_process_adds_prefix_by_default(self):
        logger = LoggerAdapter(logging.getLogger("tests.utils"))

        message, kwargs = logger.process("message", {"extra": "value"})

        assert message == "[py-netty] message"
        assert kwargs == {"extra": "value"}

    def test_process_returns_original_message_without_prefix(self):
        logger = LoggerAdapter(logging.getLogger("tests.utils"), prefix="")

        message, kwargs = logger.process("message", {})

        assert message == "message"
        assert kwargs == {}


class TestCreateThreadPool:

    def test_create_thread_pool_uses_prefix(self):
        pool = create_thread_pool(1, "custom-prefix")
        try:
            assert pool._max_workers == 1
            assert pool._thread_name_prefix == "custom-prefix"
        finally:
            pool.shutdown()
