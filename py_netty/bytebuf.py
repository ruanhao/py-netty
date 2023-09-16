from dataclasses import dataclass
from concurrent.futures import Future


@dataclass
class Chunk:

    buffer: bytes
    future: Future = None
    close: bool = False

    def __post_init__(self):
        self.future = self.future or Future()

    def __str__(self):
        return f"Chunk(bytes={len(self.buffer)}, future={self.future}, close={self.close})"

    def __repr__(self):
        return self.__str__()
