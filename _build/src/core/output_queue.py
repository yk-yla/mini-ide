"""进程输出的 FIFO：内存预算之外落临时文件，完整且按原顺序消费。"""
from __future__ import annotations

import logging
import struct
import tempfile
from collections import deque

log = logging.getLogger("mini-ide.output_queue")
_HEADER = struct.Struct("<BQ")
_STREAMS = ("stdout", "stderr", "meta")


class OutputQueue:
    def __init__(self, max_bytes=1_000_000, max_records=10_000):
        self.max_bytes, self.max_records = max_bytes, max_records
        self.memory_bytes = 0
        self._memory = deque()
        self._spool = None
        self._read_offset = 0
        self._write_offset = 0
        self._spilled = 0
        self._spill_failed = False

    def __len__(self):
        return len(self._memory) + self._spilled

    def append(self, record):
        stream, raw = record
        if self._spool is None and (
            self.memory_bytes + len(raw) <= self.max_bytes and len(self._memory) < self.max_records
        ):
            self._memory.append(record)
            self.memory_bytes += len(raw)
            return
        try:
            if self._spool is None:
                self._spool = tempfile.TemporaryFile(mode="w+b")
            self._spool.seek(self._write_offset)
            self._spool.write(_HEADER.pack(_STREAMS.index(stream), len(raw)))
            self._spool.write(raw)
            self._write_offset = self._spool.tell()
            self._spilled += 1
        except OSError:
            # 磁盘不可用时优先保留原始记录；明确报告容量保证失效。
            if not self._spill_failed:
                log.exception("输出临时文件不可用，转内存保存；请检查磁盘空间")
                self._spill_failed = True
            # 先把已有磁盘记录回收，避免新内存记录越过旧磁盘记录。
            while self._spilled:
                old = self._read_spilled()
                self._memory.append(old)
                self.memory_bytes += len(old[1])
            self._close_spool()
            self._memory.append(record)
            self.memory_bytes += len(raw)

    def popleft(self):
        if self._memory:
            record = self._memory.popleft()
            self.memory_bytes -= len(record[1])
            return record
        if self._spilled:
            record = self._read_spilled()
            if not self._spilled:
                self._close_spool()
            return record
        raise IndexError("pop from an empty output queue")

    def _read_spilled(self):
        self._spool.seek(self._read_offset)
        stream, length = _HEADER.unpack(self._spool.read(_HEADER.size))
        raw = self._spool.read(length)
        if len(raw) != length:
            raise OSError("output spool was truncated")
        self._read_offset = self._spool.tell()
        self._spilled -= 1
        return _STREAMS[stream], raw

    def _close_spool(self):
        if self._spool is not None:
            self._spool.close()
            self._spool = None
        self._read_offset = self._write_offset = 0

    def close(self):
        self._close_spool()
