from attrs import define, field
from concurrent.futures import Future

EMPTY_BUFFER = b''


@define(slots=True)
class Chunk:

    buffer: bytes = field()
    future: Future = field(default=None)
    close: bool = field(default=False)

    def __attrs_post_init__(self):
        self.future = self.future or Future()

    def __str__(self):
        return f"Chunk(bytes={len(self.buffer)}, future={self.future}, close={self.close})"

    def __repr__(self):
        return self.__str__()
