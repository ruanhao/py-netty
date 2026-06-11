from concurrent.futures import Future

from py_netty.bytebuf import EMPTY_BUFFER, Chunk


class TestChunk:

    def test_creates_future_when_not_supplied(self):
        chunk = Chunk(b"payload")

        assert isinstance(chunk.future, Future)
        assert chunk.buffer == b"payload"
        assert chunk.close is False

    def test_keeps_supplied_future_and_close_flag(self):
        future = Future()

        chunk = Chunk(EMPTY_BUFFER, future, True)

        assert chunk.future is future
        assert chunk.buffer == EMPTY_BUFFER
        assert chunk.close is True

    def test_str_and_repr_include_size_future_and_close_flag(self):
        future = Future()
        chunk = Chunk(b"abc", future, True)

        text = str(chunk)

        assert "Chunk(bytes=3" in text
        assert f"future={future}" in text
        assert "close=True" in text
        assert repr(chunk) == text
