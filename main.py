import io
import struct
from sys import stdin
from typing import Literal


class BinaryBuffer(io.BytesIO):
    def __init__(self, initial_bytes: bytes = b"") -> None:
        super().__init__(initial_bytes)

    @classmethod
    def from_stdin(cls=io.BytesIO):
        byte = io.BytesIO(stdin.buffer.read()).getvalue()
        return cls(byte)

    def position(self):
        return self.tell()

    def __len__(self):
        return self.getbuffer().__len__()

    def hex(self, pos=0, len=-1):
        return self.get_bytes(pos, len).hex().split(maxsplit=1)

    def get_bytes(self, pos=-1, len=1):
        if pos == -1:
            pos = self.tell()
        if len == -1:
            len = self.__len__() - pos

        end = pos + len
        return self.getbuffer().tobytes()[pos:end]

    def readint(self, pos=-1, byteorder: Literal["little", "big"] = "little"):
        byte = self.get_bytes(pos, 4)
        return int.from_bytes(byte, byteorder)

    def get_short(self, pos=-1):
        return self.get_bytes(pos, 2)

    def get_long(self, pos=-1):
        return self.get_bytes(pos, 2)

    def _get_string(self, pos=-1, len=1):
        """
        Leaving the default at -1 will use the current position, which might not be what you expect.
        """
        if pos == -1:
            pos = self.position()
            s = ""
            c = ""
            for p in range(pos, pos + len):
                c = str(self.get_bytes(pos, 1))
                s.__add__(c)
            return s

    def _get_string_null_terminated(self, pos=-1):
        """
        Leaving the default at -1 will use the current position, which might not be what you expect.
        """
        if pos == -1:
            pos = self.position()
        s = ""
        c = ""
        while c != "\0":
            c = str(self.get_bytes(pos, 1))
            s.__add__(c)
            if c == "\0":
                break
        return s


class DosHeader(BinaryBuffer):
    def __init__(self, initial_bytes: bytes = b"") -> None:
        super().__init__(initial_bytes)
        self.seek(0)
        self.e_magic = self.get_bytes(len=2)

        self.seek(0x3C)
        self.e_lfanew = self.readint()

    def __str__(self) -> str:
        return f"{self.e_magic}, {self.e_lfanew}"


def main():
    print("Hello, world!")

    byte = DosHeader.from_stdin()

    print("Read {} bytes".format(len(byte)))

    print(f"Dos Header: {byte:}")


main()
