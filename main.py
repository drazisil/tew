import io
from sys import stdin


class BinaryBuffer(io.BytesIO):
    def __init__(self, initial_bytes: bytes = b"") -> None:
        super().__init__(initial_bytes)

    @classmethod
    def from_stdin(cls=io.BytesIO):
        byte = io.BytesIO(stdin.buffer.read()).getvalue()
        return cls(byte)

    def __len__(self):
        return self.getbuffer().__len__()

    def get_bytes(self, pos=-1, len=1):
        if pos == -1:
            pos = self.tell()
        end = pos + len
        return self.getbuffer().tobytes()[pos:end].hex().split(maxsplit=1)


def main():
    print("Hello, world!")

    byte = BinaryBuffer.from_stdin()

    print("Read {} bytes".format(len(byte)))

    print("Byte {:} bytes".format(byte.get_bytes(pos=2, len=2)))


main()
