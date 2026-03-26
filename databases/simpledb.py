"""
simpledb.py — A Database from Scratch
======================================

A single-file educational database that implements every major concept in
database internals.  Read it top-to-bottom like a textbook:

  Layer  1  Constants & Utilities
  Layer  2  DiskManager          (raw OS I/O, page addressing)
  Layer  3  SlottedPage          (page layout, line pointers, checksums)
  Layer  4  BufferPool           (clock replacement, pin/unpin, dirty pages)
  Layer  5  WALManager           (write-ahead log, crash recovery)
  Layer  6  HeapFile             (unordered tuple storage)
  Layer  7  Tuple & Schema       (row serialization, null bitmaps, MVCC headers)
  Layer  8  B+Tree Index         (search, insert with splits, range scans)
  Layer  9  TransactionManager   (MVCC visibility, commit/abort)
  Layer 10  BloomFilter          (probabilistic membership)
  Layer 11  LSMTree              (memtable, SSTables, compaction)
  Layer 12  Query Engine          (lexer, parser, volcano-style executor)
  Layer 13  SimpleDB API         (top-level SQL interface)
  Layer 14  Demo / main          (interactive walkthrough)

Dependencies: Python 3.9+ standard library only.
"""

from __future__ import annotations

import bisect
import hashlib
import os
import struct
import tempfile
import shutil
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple as TypingTuple,
    Set,
)

# ============================================================================
# Layer 1: Constants & Utilities
# ============================================================================
# A real database aligns everything to the OS / SSD page size (typically 4 KB).
# Every read and write touches exactly one page — no more, no less.

PAGE_SIZE = 4096  # bytes — matches typical OS virtual-memory page & SSD sector

INVALID_PAGE_ID = 0xFFFFFFFF


def pack_u8(v: int) -> bytes:
    return struct.pack("!B", v)

def unpack_u8(b: bytes) -> int:
    return struct.unpack("!B", b)[0]

def pack_u16(v: int) -> bytes:
    return struct.pack("!H", v)

def unpack_u16(b: bytes) -> int:
    return struct.unpack("!H", b)[0]

def pack_u32(v: int) -> bytes:
    return struct.pack("!I", v)

def unpack_u32(b: bytes) -> int:
    return struct.unpack("!I", b)[0]

def pack_u64(v: int) -> bytes:
    return struct.pack("!Q", v)

def unpack_u64(b: bytes) -> int:
    return struct.unpack("!Q", b)[0]

def pack_i32(v: int) -> bytes:
    return struct.pack("!i", v)

def unpack_i32(b: bytes) -> int:
    return struct.unpack("!i", b)[0]

def pack_f64(v: float) -> bytes:
    return struct.pack("!d", v)

def unpack_f64(b: bytes) -> float:
    return struct.unpack("!d", b)[0]

def pack_str(s: str, max_len: int) -> bytes:
    """Length-prefixed string, padded to max_len + 2 bytes."""
    encoded = s.encode("utf-8")[:max_len]
    return pack_u16(len(encoded)) + encoded + b"\x00" * (max_len - len(encoded))

def unpack_str(b: bytes) -> str:
    length = unpack_u16(b[:2])
    return b[2:2 + length].decode("utf-8")

def crc32(data: bytes) -> int:
    """CRC-32 checksum — used by every page to detect corruption."""
    return zlib.crc32(data) & 0xFFFFFFFF


# ============================================================================
# Layer 2: DiskManager — raw, unbuffered I/O via OS syscalls
# ============================================================================
# We deliberately use os.open / os.read / os.write instead of Python's
# buffered file objects.  This mirrors what real databases do: they bypass
# the OS page cache (O_DIRECT on Linux) and manage their own buffer pool.
#
# Key idea: a page_id is simply an index.  Page 0 lives at byte offset 0,
# page 1 at 4096, page 2 at 8192, and so on.

class DiskManager:
    """Manages a single database file as a sequence of fixed-size pages."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        flags = os.O_RDWR | os.O_CREAT
        self.fd = os.open(filepath, flags, 0o644)
        # How many pages currently exist in the file?
        size = os.fstat(self.fd).st_size
        self.num_pages = size // PAGE_SIZE

    # -- core operations ---------------------------------------------------

    def read_page(self, page_id: int) -> bytes:
        """Read exactly PAGE_SIZE bytes at the given page offset."""
        os.lseek(self.fd, page_id * PAGE_SIZE, os.SEEK_SET)
        data = os.read(self.fd, PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            data += b"\x00" * (PAGE_SIZE - len(data))
        return data

    def write_page(self, page_id: int, data: bytes):
        """Write exactly PAGE_SIZE bytes at the given page offset."""
        assert len(data) == PAGE_SIZE, f"Page data must be {PAGE_SIZE} bytes"
        os.lseek(self.fd, page_id * PAGE_SIZE, os.SEEK_SET)
        os.write(self.fd, data)

    def allocate_page(self) -> int:
        """Extend the file by one page and return the new page_id."""
        page_id = self.num_pages
        self.write_page(page_id, b"\x00" * PAGE_SIZE)
        self.num_pages += 1
        return page_id

    def fsync(self):
        """Force all buffered writes to durable storage (the actual disk)."""
        os.fsync(self.fd)

    def close(self):
        os.close(self.fd)

    def __del__(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


# ============================================================================
# Layer 3: SlottedPage — the internal layout of every 4 KB page
# ============================================================================
#
#  Page layout (4096 bytes):
#  ┌──────────────────────────────────────────────────┐
#  │  HEADER  (22 bytes)                              │
#  │  page_id(4) LSN(8) checksum(4) num_slots(2)     │
#  │  free_space_offset(2) flags(2)                   │
#  ├──────────────────────────────────────────────────┤
#  │  LINE POINTERS  (4 bytes each, grow →)           │
#  │  [offset(2) | length(2)] ...                     │
#  ├──────────────────────────────────────────────────┤
#  │              free space                           │
#  ├──────────────────────────────────────────────────┤
#  │  TUPLE DATA  (grows ← from end of page)          │
#  └──────────────────────────────────────────────────┘
#
# The indirection through line pointers lets us compact tuples without
# changing any external references (TIDs remain stable).

HEADER_SIZE = 22        # bytes
LINE_POINTER_SIZE = 4   # offset (2B) + length (2B)
SLOT_DEAD = 0xFFFF      # sentinel: slot has been deleted


class SlottedPage:
    """In-memory representation of a single 4 KB slotted page."""

    def __init__(self, page_id: int = 0, data: Optional[bytes] = None):
        if data is not None:
            self._parse(data)
            # The caller knows the true page_id (freshly allocated pages
            # have all-zero bytes, so the on-disk page_id field is 0).
            self.page_id = page_id
        else:
            self.page_id = page_id
            self.lsn = 0
            self.checksum = 0
            self.num_slots = 0
            self.free_space_offset = PAGE_SIZE  # tuples grow ← from here
            self.flags = 0
            self.line_pointers: List[TypingTuple[int, int]] = []  # (offset, length)
            self.raw = bytearray(PAGE_SIZE)

    # -- parsing / serialisation -------------------------------------------

    def _parse(self, data: bytes):
        self.raw = bytearray(data)
        # Detect B+Tree pages by their marker (0xBEEF at offset 0).
        # These pages manage their own format; we just store the raw bytes.
        if unpack_u16(data[0:2]) == 0xBEEF:
            self.page_id = unpack_u32(data[3:7])
            self.lsn = 0
            self.checksum = 0
            self.num_slots = 0
            self.free_space_offset = PAGE_SIZE
            self.flags = 0x8000  # raw page flag
            self.line_pointers = []
            return
        off = 0
        self.page_id = unpack_u32(data[off:off + 4]); off += 4
        self.lsn = unpack_u64(data[off:off + 8]); off += 8
        self.checksum = unpack_u32(data[off:off + 4]); off += 4
        self.num_slots = unpack_u16(data[off:off + 2]); off += 2
        self.free_space_offset = unpack_u16(data[off:off + 2]); off += 2
        self.flags = unpack_u16(data[off:off + 2]); off += 2
        # A freshly-allocated all-zero page has free_space_offset == 0.
        # Correct it to PAGE_SIZE so add_tuple() works.
        if self.free_space_offset == 0 and self.num_slots == 0:
            self.free_space_offset = PAGE_SIZE
        # Read line pointers
        self.line_pointers = []
        for _ in range(self.num_slots):
            lp_off = unpack_u16(data[off:off + 2]); off += 2
            lp_len = unpack_u16(data[off:off + 2]); off += 2
            self.line_pointers.append((lp_off, lp_len))

    def to_bytes(self) -> bytes:
        """Serialize the page to PAGE_SIZE bytes with checksum."""
        # If the page was written directly (e.g. by B+Tree), return raw as-is.
        if self.flags & 0x8000:
            return bytes(self.raw)
        buf = bytearray(self.raw)  # start from raw (preserves tuple data)
        off = 0
        buf[off:off + 4] = pack_u32(self.page_id); off += 4
        buf[off:off + 8] = pack_u64(self.lsn); off += 8
        buf[off:off + 4] = b"\x00\x00\x00\x00"; off += 4  # placeholder for checksum
        buf[off:off + 2] = pack_u16(self.num_slots); off += 2
        buf[off:off + 2] = pack_u16(self.free_space_offset); off += 2
        buf[off:off + 2] = pack_u16(self.flags); off += 2
        for lp_off, lp_len in self.line_pointers:
            buf[off:off + 2] = pack_u16(lp_off); off += 2
            buf[off:off + 2] = pack_u16(lp_len); off += 2
        # Compute checksum over everything except the checksum field itself
        cs = crc32(bytes(buf[:12]) + bytes(buf[16:]))
        buf[12:16] = pack_u32(cs)
        self.checksum = cs
        return bytes(buf)

    def verify_checksum(self) -> bool:
        data = self.to_bytes()
        stored = unpack_u32(data[12:16])
        computed = crc32(data[:12] + data[16:])
        return stored == computed

    # -- tuple operations --------------------------------------------------

    def free_space(self) -> int:
        """How many bytes are available for a new tuple + its line pointer?"""
        lp_end = HEADER_SIZE + self.num_slots * LINE_POINTER_SIZE
        return self.free_space_offset - lp_end

    def add_tuple(self, data: bytes) -> int:
        """Insert a tuple, returning its slot_id.  Returns -1 if no room."""
        needed = len(data) + LINE_POINTER_SIZE
        if self.free_space() < needed:
            return -1
        # Place tuple data just below free_space_offset
        tuple_offset = self.free_space_offset - len(data)
        self.raw[tuple_offset:tuple_offset + len(data)] = data
        self.free_space_offset = tuple_offset
        # Append line pointer
        slot_id = self.num_slots
        self.line_pointers.append((tuple_offset, len(data)))
        self.num_slots += 1
        return slot_id

    def get_tuple(self, slot_id: int) -> Optional[bytes]:
        """Return the raw bytes of a tuple, or None if deleted."""
        if slot_id >= self.num_slots:
            return None
        off, length = self.line_pointers[slot_id]
        if off == SLOT_DEAD:
            return None
        return bytes(self.raw[off:off + length])

    def delete_tuple(self, slot_id: int):
        """Mark a slot as dead (doesn't reclaim space until compact)."""
        if slot_id < self.num_slots:
            self.line_pointers[slot_id] = (SLOT_DEAD, 0)

    def compact(self):
        """Defragment: rebuild tuple data contiguously from the end."""
        new_raw = bytearray(PAGE_SIZE)
        write_pos = PAGE_SIZE
        new_pointers: List[TypingTuple[int, int]] = []
        for off, length in self.line_pointers:
            if off == SLOT_DEAD:
                new_pointers.append((SLOT_DEAD, 0))
                continue
            write_pos -= length
            new_raw[write_pos:write_pos + length] = self.raw[off:off + length]
            new_pointers.append((write_pos, length))
        self.raw = new_raw
        self.line_pointers = new_pointers
        self.free_space_offset = write_pos


# ============================================================================
# Layer 4: BufferPool — the page cache with clock replacement
# ============================================================================
# The buffer pool sits between the query engine and the disk.  Every page
# access goes through here.  If the page is already in memory ("hit"), we
# return it immediately.  Otherwise we pick a victim frame using the Clock
# algorithm, flush the victim if dirty, and load the new page from disk.
#
# Clock replacement is an approximation of LRU that avoids the overhead of
# maintaining a linked list.  Each frame has a usage_count (0-5).  The clock
# hand sweeps: if usage > 0, decrement and skip; if usage == 0, evict.

CLOCK_MAX_USAGE = 5


@dataclass
class Frame:
    page: Optional[SlottedPage] = None
    page_id: int = INVALID_PAGE_ID
    pin_count: int = 0
    dirty: bool = False
    usage_count: int = 0


class BufferPool:
    """Fixed-size page cache with Clock replacement."""

    def __init__(self, disk: DiskManager, pool_size: int = 64):
        self.disk = disk
        self.pool_size = pool_size
        self.frames: List[Frame] = [Frame() for _ in range(pool_size)]
        self.page_table: Dict[int, int] = {}  # page_id -> frame_index
        self.clock_hand = 0
        # Stats for educational output
        self.hits = 0
        self.misses = 0

    def _clock_evict(self) -> int:
        """Find a victim frame using the Clock algorithm."""
        # Up to 2 full sweeps to find a victim with pin_count == 0
        for _ in range(2 * self.pool_size):
            frame = self.frames[self.clock_hand]
            if frame.page_id == INVALID_PAGE_ID:
                return self.clock_hand
            if frame.pin_count == 0:
                if frame.usage_count == 0:
                    victim = self.clock_hand
                    self.clock_hand = (self.clock_hand + 1) % self.pool_size
                    return victim
                else:
                    frame.usage_count -= 1
            self.clock_hand = (self.clock_hand + 1) % self.pool_size
        raise RuntimeError("BufferPool: all frames are pinned, cannot evict")

    def fetch_page(self, page_id: int) -> SlottedPage:
        """Return the page, loading from disk on a cache miss."""
        # Cache hit?
        if page_id in self.page_table:
            idx = self.page_table[page_id]
            frame = self.frames[idx]
            frame.pin_count += 1
            frame.usage_count = min(frame.usage_count + 1, CLOCK_MAX_USAGE)
            self.hits += 1
            return frame.page  # type: ignore

        # Cache miss — need to load from disk
        self.misses += 1
        idx = self._clock_evict()
        frame = self.frames[idx]

        # Flush the victim if dirty
        if frame.dirty and frame.page_id != INVALID_PAGE_ID:
            self.disk.write_page(frame.page_id, frame.page.to_bytes())  # type: ignore
            frame.dirty = False

        # Remove old page from page table
        if frame.page_id != INVALID_PAGE_ID:
            del self.page_table[frame.page_id]

        # Load new page
        raw = self.disk.read_page(page_id)
        page = SlottedPage(page_id, raw)
        frame.page = page
        frame.page_id = page_id
        frame.pin_count = 1
        frame.usage_count = 1
        frame.dirty = False
        self.page_table[page_id] = idx
        return page

    def unpin(self, page_id: int, dirty: bool = False):
        if page_id in self.page_table:
            frame = self.frames[self.page_table[page_id]]
            if frame.pin_count > 0:
                frame.pin_count -= 1
            if dirty:
                frame.dirty = True

    def flush_page(self, page_id: int):
        if page_id in self.page_table:
            idx = self.page_table[page_id]
            frame = self.frames[idx]
            if frame.dirty and frame.page is not None:
                self.disk.write_page(page_id, frame.page.to_bytes())
                frame.dirty = False

    def flush_all(self):
        for frame in self.frames:
            if frame.dirty and frame.page is not None:
                self.disk.write_page(frame.page_id, frame.page.to_bytes())
                frame.dirty = False
        self.disk.fsync()

    def new_page(self) -> SlottedPage:
        """Allocate a fresh page on disk and load it into the pool."""
        page_id = self.disk.allocate_page()
        return self.fetch_page(page_id)


# ============================================================================
# Layer 5: WALManager — Write-Ahead Logging
# ============================================================================
# The WAL is the foundation of crash safety.  The rule is simple:
#   **No dirty page may be flushed to the data file until its WAL record
#   is on stable storage.**
#
# WAL record format (binary):
#   LSN (8B) | txn_id (4B) | prev_lsn (8B) | record_type (1B) |
#   page_id (4B) | payload_len (2B) | payload (variable)
#
# The total header is 27 bytes before the payload.

class WALRecordType(Enum):
    BEGIN      = 0
    INSERT     = 1
    UPDATE     = 2
    DELETE     = 3
    COMMIT     = 4
    ABORT      = 5
    CHECKPOINT = 6

WAL_HEADER_SIZE = 27  # 8 + 4 + 8 + 1 + 4 + 2


@dataclass
class WALRecord:
    lsn: int
    txn_id: int
    prev_lsn: int
    record_type: WALRecordType
    page_id: int
    payload: bytes


class WALManager:
    """Append-only binary write-ahead log."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
        self.fd = os.open(filepath, flags, 0o644)
        self.current_lsn = 0
        # Recover existing LSN counter
        size = os.fstat(self.fd).st_size
        if size > 0:
            self._scan_for_max_lsn()

    def _scan_for_max_lsn(self):
        """Read through the log to find the highest LSN."""
        os.lseek(self.fd, 0, os.SEEK_SET)
        file_size = os.fstat(self.fd).st_size
        pos = 0
        while pos + WAL_HEADER_SIZE <= file_size:
            os.lseek(self.fd, pos, os.SEEK_SET)
            header = os.read(self.fd, WAL_HEADER_SIZE)
            if len(header) < WAL_HEADER_SIZE:
                break
            lsn = unpack_u64(header[0:8])
            payload_len = unpack_u16(header[25:27])
            self.current_lsn = lsn + 1
            pos += WAL_HEADER_SIZE + payload_len

    def append(self, txn_id: int, record_type: WALRecordType,
               page_id: int, payload: bytes = b"", prev_lsn: int = 0) -> int:
        """Append a WAL record and return its LSN."""
        lsn = self.current_lsn
        self.current_lsn += 1
        record = (
            pack_u64(lsn)
            + pack_u32(txn_id)
            + pack_u64(prev_lsn)
            + pack_u8(record_type.value)
            + pack_u32(page_id)
            + pack_u16(len(payload))
            + payload
        )
        os.write(self.fd, record)
        return lsn

    def flush(self):
        """Force WAL to stable storage (group commit point)."""
        os.fsync(self.fd)

    def recover(self) -> List[WALRecord]:
        """Read all WAL records — used during crash recovery."""
        records: List[WALRecord] = []
        os.lseek(self.fd, 0, os.SEEK_SET)
        file_size = os.fstat(self.fd).st_size
        pos = 0
        while pos + WAL_HEADER_SIZE <= file_size:
            os.lseek(self.fd, pos, os.SEEK_SET)
            header = os.read(self.fd, WAL_HEADER_SIZE)
            if len(header) < WAL_HEADER_SIZE:
                break
            lsn = unpack_u64(header[0:8])
            txn_id = unpack_u32(header[8:12])
            prev_lsn = unpack_u64(header[12:20])
            rtype = WALRecordType(unpack_u8(header[20:21]))
            page_id = unpack_u32(header[21:25])
            payload_len = unpack_u16(header[25:27])
            payload = b""
            if payload_len > 0:
                payload = os.read(self.fd, payload_len)
            records.append(WALRecord(lsn, txn_id, prev_lsn, rtype, page_id, payload))
            pos += WAL_HEADER_SIZE + payload_len
        return records

    def checkpoint(self, dirty_page_ids: List[int]) -> int:
        """Write a checkpoint record listing all dirty pages."""
        payload = b"".join(pack_u32(pid) for pid in dirty_page_ids)
        return self.append(0, WALRecordType.CHECKPOINT, 0, payload)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __del__(self):
        self.close()


# ============================================================================
# Layer 6: HeapFile — unordered collection of tuples
# ============================================================================
# A heap file is the simplest table storage: tuples are appended wherever
# there's room.  There's no sort order — that's what indexes are for.
# We maintain a simple free-space map (FSM) so we can quickly find a page
# with enough room for a new tuple.

class HeapFile:
    """Manages a collection of SlottedPages through the BufferPool."""

    def __init__(self, buffer_pool: BufferPool, start_page_id: int = 0):
        self.pool = buffer_pool
        self.start_page_id = start_page_id
        # Free-space map: page_id -> approximate free bytes
        self.fsm: Dict[int, int] = {}
        self.page_count = 0

    def _ensure_page(self) -> int:
        """Create a new heap page and register it in the FSM."""
        page = self.pool.new_page()
        pid = page.page_id
        self.fsm[pid] = page.free_space()
        self.pool.unpin(pid)
        self.page_count += 1
        return pid

    def insert(self, tuple_data: bytes) -> TypingTuple[int, int]:
        """Insert a tuple, returning (page_id, slot_id)."""
        needed = len(tuple_data) + LINE_POINTER_SIZE
        # Find a page with enough space
        target_pid = None
        for pid, free in self.fsm.items():
            if free >= needed:
                target_pid = pid
                break
        if target_pid is None:
            target_pid = self._ensure_page()

        page = self.pool.fetch_page(target_pid)
        slot_id = page.add_tuple(tuple_data)
        if slot_id == -1:
            # FSM was stale — try a new page
            self.pool.unpin(target_pid)
            target_pid = self._ensure_page()
            page = self.pool.fetch_page(target_pid)
            slot_id = page.add_tuple(tuple_data)
        self.fsm[target_pid] = page.free_space()
        self.pool.unpin(target_pid, dirty=True)
        return (target_pid, slot_id)

    def delete(self, page_id: int, slot_id: int):
        page = self.pool.fetch_page(page_id)
        page.delete_tuple(slot_id)
        self.fsm[page_id] = page.free_space()
        self.pool.unpin(page_id, dirty=True)

    def get(self, page_id: int, slot_id: int) -> Optional[bytes]:
        page = self.pool.fetch_page(page_id)
        data = page.get_tuple(slot_id)
        self.pool.unpin(page_id)
        return data

    def full_scan(self) -> Iterator[TypingTuple[int, int, bytes]]:
        """Yield (page_id, slot_id, data) for every live tuple."""
        for pid in sorted(self.fsm.keys()):
            page = self.pool.fetch_page(pid)
            for sid in range(page.num_slots):
                data = page.get_tuple(sid)
                if data is not None:
                    yield (pid, sid, data)
            self.pool.unpin(pid)


# ============================================================================
# Layer 7: Tuple & Schema — row format and serialization
# ============================================================================
# Every row stored in a heap page is a serialized tuple.  The tuple header
# carries MVCC metadata (xmin/xmax) so the TransactionManager can decide
# visibility.  A null bitmap follows, then the column values.

class ColumnType(Enum):
    INTEGER  = auto()
    FLOAT    = auto()
    VARCHAR  = auto()
    BOOLEAN  = auto()


@dataclass
class Column:
    name: str
    col_type: ColumnType
    max_length: int = 0   # only meaningful for VARCHAR
    nullable: bool = True


@dataclass
class Schema:
    columns: List[Column]

    def col_index(self, name: str) -> int:
        for i, c in enumerate(self.columns):
            if c.name == name:
                return i
        raise KeyError(f"No column '{name}'")


# Tuple binary layout:
#   xmin (4B) | xmax (4B) | null_bitmap (ceil(ncols/8) bytes) | col values...

TUPLE_HEADER_SIZE = 8  # xmin + xmax


def _null_bitmap_size(ncols: int) -> int:
    return (ncols + 7) // 8


def serialize_tuple(values: List[Any], schema: Schema,
                    xmin: int = 0, xmax: int = 0) -> bytes:
    """Serialize a row (list of Python values) into bytes."""
    ncols = len(schema.columns)
    bitmap_size = _null_bitmap_size(ncols)
    bitmap = bytearray(bitmap_size)

    parts: List[bytes] = []
    for i, (val, col) in enumerate(zip(values, schema.columns)):
        if val is None:
            bitmap[i // 8] |= (1 << (i % 8))
            continue
        if col.col_type == ColumnType.INTEGER:
            parts.append(pack_i32(val))
        elif col.col_type == ColumnType.FLOAT:
            parts.append(pack_f64(val))
        elif col.col_type == ColumnType.VARCHAR:
            parts.append(pack_str(str(val), col.max_length))
        elif col.col_type == ColumnType.BOOLEAN:
            parts.append(pack_u8(1 if val else 0))

    return pack_u32(xmin) + pack_u32(xmax) + bytes(bitmap) + b"".join(parts)


def deserialize_tuple(data: bytes, schema: Schema) -> dict:
    """Deserialize bytes back into a dict of column_name -> value."""
    ncols = len(schema.columns)
    bitmap_size = _null_bitmap_size(ncols)
    xmin = unpack_u32(data[0:4])
    xmax = unpack_u32(data[4:8])
    bitmap = data[8:8 + bitmap_size]
    off = 8 + bitmap_size

    result: dict = {"__xmin": xmin, "__xmax": xmax}
    for i, col in enumerate(schema.columns):
        is_null = (bitmap[i // 8] >> (i % 8)) & 1
        if is_null:
            result[col.name] = None
            continue
        if col.col_type == ColumnType.INTEGER:
            result[col.name] = unpack_i32(data[off:off + 4]); off += 4
        elif col.col_type == ColumnType.FLOAT:
            result[col.name] = unpack_f64(data[off:off + 8]); off += 8
        elif col.col_type == ColumnType.VARCHAR:
            s = unpack_str(data[off:]); off += 2 + col.max_length
            result[col.name] = s
        elif col.col_type == ColumnType.BOOLEAN:
            result[col.name] = bool(unpack_u8(data[off:off + 1])); off += 1
    return result


# ============================================================================
# Layer 8: B+Tree Index
# ============================================================================
# A B+Tree is the workhorse index of every OLTP database.  Internal nodes
# store keys + child pointers; leaf nodes store keys + record IDs (page_id,
# slot_id).  Leaves are linked for efficient range scans.
#
# We store each node as a page in the buffer pool, which means the B+Tree
# is automatically cached and durable.
#
#  Internal node layout (in-memory):
#    keys:     [k0, k1, ..., k_{n-1}]
#    children: [c0, c1, ..., c_n]       (page_ids)
#    Invariant: all keys in subtree c_i < k_i <= all keys in subtree c_{i+1}
#
#  Leaf node layout (in-memory):
#    keys:   [k0, k1, ...]
#    values: [(page_id, slot_id), ...]
#    next_leaf: page_id of the right sibling (for range scans)

BTREE_INTERNAL = 0
BTREE_LEAF = 1

# Serialisation markers inside pages
_BTREE_PAGE_MARKER = 0xBEEF


@dataclass
class BTreeNode:
    """In-memory representation of a B+Tree node."""
    page_id: int
    is_leaf: bool
    keys: List[Any] = field(default_factory=list)
    # For leaves: list of (page_id, slot_id)
    values: List[TypingTuple[int, int]] = field(default_factory=list)
    # For internals: child page_ids
    children: List[int] = field(default_factory=list)
    # Leaf linked-list pointer
    next_leaf: int = INVALID_PAGE_ID

    def serialize(self) -> bytes:
        """Pack the node into PAGE_SIZE bytes."""
        buf = bytearray(PAGE_SIZE)
        off = 0
        buf[off:off + 2] = pack_u16(_BTREE_PAGE_MARKER); off += 2
        buf[off:off + 1] = pack_u8(BTREE_LEAF if self.is_leaf else BTREE_INTERNAL); off += 1
        buf[off:off + 4] = pack_u32(self.page_id); off += 4
        nkeys = len(self.keys)
        buf[off:off + 2] = pack_u16(nkeys); off += 2

        if self.is_leaf:
            buf[off:off + 4] = pack_u32(self.next_leaf); off += 4
            for i in range(nkeys):
                buf[off:off + 4] = pack_i32(self.keys[i]); off += 4
                pid, sid = self.values[i]
                buf[off:off + 4] = pack_u32(pid); off += 4
                buf[off:off + 2] = pack_u16(sid); off += 2
        else:
            for i in range(nkeys):
                buf[off:off + 4] = pack_i32(self.keys[i]); off += 4
            for i in range(nkeys + 1):
                buf[off:off + 4] = pack_u32(self.children[i]); off += 4
        return bytes(buf)

    @staticmethod
    def deserialize(data: bytes) -> "BTreeNode":
        off = 0
        marker = unpack_u16(data[off:off + 2]); off += 2
        if marker != _BTREE_PAGE_MARKER:
            # Not a btree page — return empty leaf
            return BTreeNode(page_id=0, is_leaf=True)
        is_leaf = unpack_u8(data[off:off + 1]) == BTREE_LEAF; off += 1
        page_id = unpack_u32(data[off:off + 4]); off += 4
        nkeys = unpack_u16(data[off:off + 2]); off += 2

        node = BTreeNode(page_id=page_id, is_leaf=is_leaf)
        if is_leaf:
            node.next_leaf = unpack_u32(data[off:off + 4]); off += 4
            for _ in range(nkeys):
                key = unpack_i32(data[off:off + 4]); off += 4
                pid = unpack_u32(data[off:off + 4]); off += 4
                sid = unpack_u16(data[off:off + 2]); off += 2
                node.keys.append(key)
                node.values.append((pid, sid))
        else:
            for _ in range(nkeys):
                node.keys.append(unpack_i32(data[off:off + 4])); off += 4
            for _ in range(nkeys + 1):
                node.children.append(unpack_u32(data[off:off + 4])); off += 4
        return node


class BPlusTree:
    """
    Disk-backed B+Tree index.

    `order` is the maximum number of children for internal nodes (fan-out).
    A leaf holds at most order-1 key/value pairs.
    """

    def __init__(self, buffer_pool: BufferPool, order: int = 32):
        self.pool = buffer_pool
        self.order = order
        self.max_keys = order - 1  # max keys per node
        self.root_page_id: Optional[int] = None

    # -- helpers -----------------------------------------------------------

    def _read_node(self, page_id: int) -> BTreeNode:
        page = self.pool.fetch_page(page_id)
        node = BTreeNode.deserialize(bytes(page.raw))
        node.page_id = page_id  # authoritative page_id from buffer pool
        self.pool.unpin(page_id)
        return node

    def _write_node(self, node: BTreeNode):
        page = self.pool.fetch_page(node.page_id)
        data = node.serialize()
        page.raw = bytearray(data)
        page.flags = 0x8000  # mark as raw page (skip slotted-page header rebuild)
        self.pool.unpin(node.page_id, dirty=True)

    def _alloc_node(self, is_leaf: bool) -> BTreeNode:
        page = self.pool.new_page()
        node = BTreeNode(page_id=page.page_id, is_leaf=is_leaf)
        self.pool.unpin(page.page_id)
        return node

    # -- search ------------------------------------------------------------

    def search(self, key: int) -> Optional[TypingTuple[int, int]]:
        """Find the record ID for an exact key match, or None."""
        if self.root_page_id is None:
            return None
        node = self._read_node(self.root_page_id)
        while not node.is_leaf:
            # Find child to descend into
            idx = bisect.bisect_right(node.keys, key)
            node = self._read_node(node.children[idx])
        # Search within leaf
        idx = bisect.bisect_left(node.keys, key)
        if idx < len(node.keys) and node.keys[idx] == key:
            return node.values[idx]
        return None

    def range_scan(self, low: int, high: int) -> Iterator[TypingTuple[int, TypingTuple[int, int]]]:
        """Yield (key, (page_id, slot_id)) for all keys in [low, high]."""
        if self.root_page_id is None:
            return
        # Navigate to leftmost leaf that could contain `low`
        node = self._read_node(self.root_page_id)
        while not node.is_leaf:
            idx = bisect.bisect_left(node.keys, low)
            node = self._read_node(node.children[idx])
        # Scan leaves via the linked list
        while True:
            for i, k in enumerate(node.keys):
                if k > high:
                    return
                if k >= low:
                    yield (k, node.values[i])
            if node.next_leaf == INVALID_PAGE_ID:
                return
            node = self._read_node(node.next_leaf)

    # -- insert with split -------------------------------------------------

    def insert(self, key: int, rid: TypingTuple[int, int]):
        """Insert a key/record-id pair, splitting nodes as necessary."""
        if self.root_page_id is None:
            # First insert: create root leaf
            root = self._alloc_node(is_leaf=True)
            root.keys.append(key)
            root.values.append(rid)
            self._write_node(root)
            self.root_page_id = root.page_id
            return

        result = self._insert_recursive(self.root_page_id, key, rid)
        if result is not None:
            # Root was split — create new root
            split_key, new_page_id = result
            new_root = self._alloc_node(is_leaf=False)
            new_root.keys.append(split_key)
            new_root.children.append(self.root_page_id)
            new_root.children.append(new_page_id)
            self._write_node(new_root)
            self.root_page_id = new_root.page_id

    def _insert_recursive(self, page_id: int, key: int,
                          rid: TypingTuple[int, int]) -> Optional[TypingTuple[int, int]]:
        """Insert into subtree rooted at page_id.
        Returns (split_key, new_page_id) if the node was split, else None."""
        node = self._read_node(page_id)

        if node.is_leaf:
            # Insert into leaf
            idx = bisect.bisect_left(node.keys, key)
            # Update if key already exists
            if idx < len(node.keys) and node.keys[idx] == key:
                node.values[idx] = rid
                self._write_node(node)
                return None
            node.keys.insert(idx, key)
            node.values.insert(idx, rid)
            if len(node.keys) <= self.max_keys:
                self._write_node(node)
                return None
            # Split leaf
            return self._split_leaf(node)

        else:
            # Internal node: descend
            idx = bisect.bisect_right(node.keys, key)
            result = self._insert_recursive(node.children[idx], key, rid)
            if result is None:
                return None
            split_key, new_child_pid = result
            # Insert the promoted key into this internal node
            node.keys.insert(idx, split_key)
            node.children.insert(idx + 1, new_child_pid)
            if len(node.keys) <= self.max_keys:
                self._write_node(node)
                return None
            # Split internal node
            return self._split_internal(node)

    def _split_leaf(self, node: BTreeNode) -> TypingTuple[int, int]:
        mid = len(node.keys) // 2
        new_leaf = self._alloc_node(is_leaf=True)
        new_leaf.keys = node.keys[mid:]
        new_leaf.values = node.values[mid:]
        new_leaf.next_leaf = node.next_leaf
        node.keys = node.keys[:mid]
        node.values = node.values[:mid]
        node.next_leaf = new_leaf.page_id
        self._write_node(node)
        self._write_node(new_leaf)
        return (new_leaf.keys[0], new_leaf.page_id)

    def _split_internal(self, node: BTreeNode) -> TypingTuple[int, int]:
        mid = len(node.keys) // 2
        promote_key = node.keys[mid]
        new_internal = self._alloc_node(is_leaf=False)
        new_internal.keys = node.keys[mid + 1:]
        new_internal.children = node.children[mid + 1:]
        node.keys = node.keys[:mid]
        node.children = node.children[:mid + 1]
        self._write_node(node)
        self._write_node(new_internal)
        return (promote_key, new_internal.page_id)

    def delete(self, key: int):
        """Lazy delete: remove key from leaf (no rebalancing)."""
        if self.root_page_id is None:
            return
        node = self._read_node(self.root_page_id)
        while not node.is_leaf:
            idx = bisect.bisect_right(node.keys, key)
            node = self._read_node(node.children[idx])
        idx = bisect.bisect_left(node.keys, key)
        if idx < len(node.keys) and node.keys[idx] == key:
            node.keys.pop(idx)
            node.values.pop(idx)
            self._write_node(node)


# ============================================================================
# Layer 9: TransactionManager & MVCC
# ============================================================================
# MVCC (Multi-Version Concurrency Control) lets readers and writers work
# without blocking each other.  Each tuple carries xmin (the txn that
# created it) and xmax (the txn that deleted it, or 0).
#
# Visibility rule (Read Committed):
#   A tuple is visible to txn T if:
#     xmin is committed  AND  (xmax == 0  OR  xmax is NOT committed)

class TxnStatus(Enum):
    ACTIVE    = auto()
    COMMITTED = auto()
    ABORTED   = auto()


class TransactionManager:
    """Manages transactions with MVCC visibility."""

    def __init__(self, wal: WALManager):
        self.wal = wal
        self.next_txn_id = 1
        self.txn_status: Dict[int, TxnStatus] = {}
        self.active_txns: Set[int] = set()
        # prev_lsn per transaction for undo chains
        self.txn_prev_lsn: Dict[int, int] = {}

    def begin(self) -> int:
        txn_id = self.next_txn_id
        self.next_txn_id += 1
        self.txn_status[txn_id] = TxnStatus.ACTIVE
        self.active_txns.add(txn_id)
        self.txn_prev_lsn[txn_id] = 0
        lsn = self.wal.append(txn_id, WALRecordType.BEGIN, 0,
                              prev_lsn=0)
        self.txn_prev_lsn[txn_id] = lsn
        return txn_id

    def commit(self, txn_id: int):
        prev = self.txn_prev_lsn.get(txn_id, 0)
        self.wal.append(txn_id, WALRecordType.COMMIT, 0, prev_lsn=prev)
        self.wal.flush()
        self.txn_status[txn_id] = TxnStatus.COMMITTED
        self.active_txns.discard(txn_id)

    def abort(self, txn_id: int):
        prev = self.txn_prev_lsn.get(txn_id, 0)
        self.wal.append(txn_id, WALRecordType.ABORT, 0, prev_lsn=prev)
        self.wal.flush()
        self.txn_status[txn_id] = TxnStatus.ABORTED
        self.active_txns.discard(txn_id)

    def log_write(self, txn_id: int, record_type: WALRecordType,
                  page_id: int, payload: bytes = b"") -> int:
        prev = self.txn_prev_lsn.get(txn_id, 0)
        lsn = self.wal.append(txn_id, record_type, page_id, payload, prev)
        self.txn_prev_lsn[txn_id] = lsn
        return lsn

    def is_committed(self, txn_id: int) -> bool:
        return self.txn_status.get(txn_id) == TxnStatus.COMMITTED

    def is_visible(self, xmin: int, xmax: int, reading_txn: int = 0) -> bool:
        """MVCC visibility check (Read Committed).

        A tuple is visible if:
          1. xmin is committed (the inserting txn finished), AND
          2. xmax == 0 (not deleted) OR xmax's txn is not committed.
        """
        if not self.is_committed(xmin):
            # The row's creator hasn't committed — only visible to itself
            if xmin != reading_txn:
                return False
        if xmax == 0:
            return True
        if self.is_committed(xmax):
            return False  # deleted and the delete committed
        return True  # delete hasn't committed — row is still visible

    def recover(self) -> TypingTuple[Set[int], Set[int]]:
        """Crash recovery: replay WAL and determine committed/aborted txns."""
        records = self.wal.recover()
        committed: Set[int] = set()
        began: Set[int] = set()
        for rec in records:
            if rec.record_type == WALRecordType.BEGIN:
                began.add(rec.txn_id)
            elif rec.record_type == WALRecordType.COMMIT:
                committed.add(rec.txn_id)
        # Transactions that began but never committed must be aborted
        aborted = began - committed
        for txn_id in committed:
            self.txn_status[txn_id] = TxnStatus.COMMITTED
        for txn_id in aborted:
            self.txn_status[txn_id] = TxnStatus.ABORTED
        return committed, aborted


# ============================================================================
# Layer 10: Bloom Filter
# ============================================================================
# A Bloom filter is a space-efficient probabilistic set.  It can tell you
# "definitely not in the set" or "maybe in the set".  LSM trees use bloom
# filters to avoid reading SSTables that definitely don't contain a key.

class BloomFilter:
    """Bloom filter with k hash functions derived from MD5."""

    def __init__(self, capacity: int = 1000, fp_rate: float = 0.01):
        import math
        # Optimal number of bits and hash functions
        self.size = max(1, int(-capacity * math.log(fp_rate) / (math.log(2) ** 2)))
        self.k = max(1, int((self.size / capacity) * math.log(2)))
        self.bits = bytearray(self.size)

    def _hashes(self, key: str) -> List[int]:
        """Generate k hash positions using double-hashing from MD5."""
        digest = hashlib.md5(key.encode()).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big")
        return [(h1 + i * h2) % self.size for i in range(self.k)]

    def add(self, key: str):
        for pos in self._hashes(key):
            self.bits[pos] = 1

    def might_contain(self, key: str) -> bool:
        return all(self.bits[pos] for pos in self._hashes(key))


# ============================================================================
# Layer 11: LSM Tree — Log-Structured Merge Tree
# ============================================================================
# LSM trees optimise for write throughput by buffering writes in memory
# (MemTable) and flushing sorted runs to disk (SSTables).  Reads check
# the MemTable first, then SSTables from newest to oldest, using bloom
# filters to skip tables that can't contain the key.
#
# Write path:  put(k,v) -> WAL -> MemTable -> flush to SSTable when full
# Read path:   MemTable -> L0 SSTables (newest first) -> L1 -> ...
#
# Compaction merges overlapping SSTables to reclaim space and bound read
# amplification (simplified leveled compaction).

_TOMBSTONE = b"__TOMBSTONE__"


class MemTable:
    """In-memory sorted key-value store (backed by a Python dict + sorted keys list)."""

    def __init__(self, max_size: int = 256):
        self.data: Dict[str, bytes] = {}
        self.max_size = max_size
        self._sorted_keys: Optional[List[str]] = None

    def put(self, key: str, value: bytes):
        self.data[key] = value
        self._sorted_keys = None  # invalidate cache

    def get(self, key: str) -> Optional[bytes]:
        return self.data.get(key)

    def delete(self, key: str):
        self.data[key] = _TOMBSTONE
        self._sorted_keys = None

    def is_full(self) -> bool:
        return len(self.data) >= self.max_size

    def sorted_items(self) -> List[TypingTuple[str, bytes]]:
        if self._sorted_keys is None:
            self._sorted_keys = sorted(self.data.keys())
        return [(k, self.data[k]) for k in self._sorted_keys]

    def clear(self):
        self.data.clear()
        self._sorted_keys = None


class SSTable:
    """
    Sorted String Table on disk.

    File format:
      [num_entries (4B)]
      [bloom filter size (4B)] [bloom bits...]
      [index: key_len(2B) key(var) offset(4B)] * num_entries
      [data:  value_len(4B) value(var)] * num_entries
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.bloom = BloomFilter(capacity=1000)
        self.index: List[TypingTuple[str, int]] = []  # (key, data_offset)
        self._keys: List[str] = []

    @staticmethod
    def create(filepath: str, items: List[TypingTuple[str, bytes]]) -> "SSTable":
        """Flush sorted items to a new SSTable file."""
        sst = SSTable(filepath)
        sst.bloom = BloomFilter(capacity=max(len(items), 1))

        # Build bloom filter
        for key, _ in items:
            sst.bloom.add(key)

        # Serialise: we write index block then data block
        index_entries: List[bytes] = []
        data_entries: List[bytes] = []
        data_offset = 0
        for key, value in items:
            kb = key.encode("utf-8")
            index_entries.append(pack_u16(len(kb)) + kb + pack_u32(data_offset))
            val_entry = pack_u32(len(value)) + value
            data_entries.append(val_entry)
            data_offset += len(val_entry)
            sst.index.append((key, data_offset))
            sst._keys.append(key)

        bloom_bytes = bytes(sst.bloom.bits)
        with open(filepath, "wb") as f:
            f.write(pack_u32(len(items)))
            f.write(pack_u32(len(bloom_bytes)))
            f.write(bloom_bytes)
            for entry in index_entries:
                f.write(entry)
            for entry in data_entries:
                f.write(entry)
        return sst

    def load_index(self):
        """Load bloom filter and index from disk (not the data)."""
        with open(self.filepath, "rb") as f:
            num_entries = unpack_u32(f.read(4))
            bloom_size = unpack_u32(f.read(4))
            bloom_bits = f.read(bloom_size)
            self.bloom = BloomFilter(capacity=max(num_entries, 1))
            self.bloom.bits = bytearray(bloom_bits)
            self.bloom.size = bloom_size
            self.index = []
            self._keys = []
            for _ in range(num_entries):
                klen = unpack_u16(f.read(2))
                key = f.read(klen).decode("utf-8")
                _off = unpack_u32(f.read(4))
                self.index.append((key, _off))
                self._keys.append(key)
            self._data_start = f.tell()

    def get(self, key: str) -> Optional[bytes]:
        """Look up a key — returns None if not found or tombstone."""
        if not self.bloom.might_contain(key):
            return None  # Bloom says definitely not here
        # Binary search in the index
        idx = bisect.bisect_left(self._keys, key)
        if idx >= len(self._keys) or self._keys[idx] != key:
            return None
        # Read value from data block
        with open(self.filepath, "rb") as f:
            # Skip header
            num_entries = len(self.index)
            f.read(4)  # num_entries
            bloom_size = unpack_u32(f.read(4))
            f.read(bloom_size)  # bloom bits
            # Skip index entries
            for _ in range(num_entries):
                klen = unpack_u16(f.read(2))
                f.read(klen + 4)
            # Now at data block start — seek to the idx-th entry
            for i in range(idx):
                vlen = unpack_u32(f.read(4))
                f.read(vlen)
            vlen = unpack_u32(f.read(4))
            value = f.read(vlen)
        if value == _TOMBSTONE:
            return None
        return value

    def all_items(self) -> List[TypingTuple[str, bytes]]:
        """Read all key-value pairs (for compaction)."""
        items: List[TypingTuple[str, bytes]] = []
        with open(self.filepath, "rb") as f:
            num_entries = unpack_u32(f.read(4))
            bloom_size = unpack_u32(f.read(4))
            f.read(bloom_size)
            keys = []
            for _ in range(num_entries):
                klen = unpack_u16(f.read(2))
                key = f.read(klen).decode("utf-8")
                f.read(4)  # offset
                keys.append(key)
            for key in keys:
                vlen = unpack_u32(f.read(4))
                value = f.read(vlen)
                items.append((key, value))
        return items


class LSMTree:
    """Log-Structured Merge Tree with leveled compaction."""

    def __init__(self, directory: str, memtable_size: int = 256):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.memtable = MemTable(max_size=memtable_size)
        self.sstables: List[SSTable] = []  # newest first
        self._sst_counter = 0
        self._load_existing_sstables()

    def _load_existing_sstables(self):
        """Reload SSTable index from disk on startup."""
        files = sorted(
            [f for f in os.listdir(self.directory) if f.endswith(".sst")],
            reverse=True,
        )
        for fname in files:
            sst = SSTable(os.path.join(self.directory, fname))
            sst.load_index()
            self.sstables.append(sst)
            num = int(fname.replace("sst_", "").replace(".sst", ""))
            self._sst_counter = max(self._sst_counter, num + 1)

    def put(self, key: str, value: bytes):
        self.memtable.put(key, value)
        if self.memtable.is_full():
            self._flush()

    def get(self, key: str) -> Optional[bytes]:
        # 1. Check MemTable (most recent writes)
        val = self.memtable.get(key)
        if val is not None:
            return None if val == _TOMBSTONE else val
        # 2. Check SSTables from newest to oldest
        for sst in self.sstables:
            val = sst.get(key)
            if val is not None:
                return val
        return None

    def delete(self, key: str):
        """Write a tombstone marker."""
        self.memtable.delete(key)
        if self.memtable.is_full():
            self._flush()

    def _flush(self):
        """Flush the MemTable to a new SSTable."""
        items = self.memtable.sorted_items()
        if not items:
            return
        path = os.path.join(self.directory, f"sst_{self._sst_counter:06d}.sst")
        sst = SSTable.create(path, items)
        sst.load_index()
        self.sstables.insert(0, sst)  # newest first
        self._sst_counter += 1
        self.memtable.clear()
        # Trigger compaction if too many L0 SSTables
        if len(self.sstables) >= 4:
            self._compact()

    def _compact(self):
        """Merge all SSTables into one (simplified full compaction)."""
        merged: Dict[str, bytes] = {}
        # Oldest first so newer values overwrite older
        for sst in reversed(self.sstables):
            for key, value in sst.all_items():
                merged[key] = value
        # Remove tombstones
        live = [(k, v) for k, v in sorted(merged.items()) if v != _TOMBSTONE]
        # Delete old files
        for sst in self.sstables:
            try:
                os.remove(sst.filepath)
            except OSError:
                pass
        self.sstables.clear()
        if live:
            path = os.path.join(self.directory, f"sst_{self._sst_counter:06d}.sst")
            sst = SSTable.create(path, live)
            sst.load_index()
            self.sstables.append(sst)
            self._sst_counter += 1

    def force_flush(self):
        """Force-flush the MemTable (useful for testing)."""
        self._flush()


# ============================================================================
# Layer 12: Simple Query Engine — Lexer, Parser, Executor
# ============================================================================
# A SQL query goes through three stages:
#   1. Lexer:    SQL string -> list of tokens
#   2. Parser:   tokens -> AST (abstract syntax tree)
#   3. Executor: AST -> result rows (Volcano-style iterators)

# -- 12a: Lexer ------------------------------------------------------------

class TokenType(Enum):
    # Keywords
    SELECT     = auto()
    INSERT     = auto()
    INTO       = auto()
    VALUES     = auto()
    DELETE     = auto()
    FROM       = auto()
    WHERE      = auto()
    CREATE     = auto()
    TABLE      = auto()
    INDEX      = auto()
    ON         = auto()
    AND        = auto()
    OR         = auto()
    BEGIN_KW   = auto()
    COMMIT_KW  = auto()
    ROLLBACK   = auto()
    INTEGER_KW = auto()
    FLOAT_KW   = auto()
    VARCHAR_KW = auto()
    BOOLEAN_KW = auto()
    UPDATE_KW  = auto()
    SET_KW     = auto()
    # Literals & identifiers
    NUMBER     = auto()
    FLOAT_LIT  = auto()
    STRING     = auto()
    IDENT      = auto()
    # Symbols
    STAR       = auto()
    COMMA      = auto()
    LPAREN     = auto()
    RPAREN     = auto()
    SEMICOLON  = auto()
    EQ         = auto()
    NEQ        = auto()
    LT         = auto()
    GT         = auto()
    LTE        = auto()
    GTE        = auto()
    # Special
    EOF        = auto()


KEYWORDS = {
    "SELECT": TokenType.SELECT, "INSERT": TokenType.INSERT,
    "INTO": TokenType.INTO, "VALUES": TokenType.VALUES,
    "DELETE": TokenType.DELETE, "FROM": TokenType.FROM,
    "WHERE": TokenType.WHERE, "CREATE": TokenType.CREATE,
    "TABLE": TokenType.TABLE, "INDEX": TokenType.INDEX,
    "ON": TokenType.ON, "AND": TokenType.AND, "OR": TokenType.OR,
    "BEGIN": TokenType.BEGIN_KW, "COMMIT": TokenType.COMMIT_KW,
    "ROLLBACK": TokenType.ROLLBACK,
    "INTEGER": TokenType.INTEGER_KW, "INT": TokenType.INTEGER_KW,
    "FLOAT": TokenType.FLOAT_KW, "DOUBLE": TokenType.FLOAT_KW,
    "VARCHAR": TokenType.VARCHAR_KW,
    "BOOLEAN": TokenType.BOOLEAN_KW, "BOOL": TokenType.BOOLEAN_KW,
    "UPDATE": TokenType.UPDATE_KW, "SET": TokenType.SET_KW,
}


@dataclass
class Token:
    type: TokenType
    value: str


def lex(sql: str) -> List[Token]:
    """Tokenise a SQL string."""
    tokens: List[Token] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        # Skip whitespace
        if ch.isspace():
            i += 1; continue
        # String literal
        if ch == "'":
            j = i + 1
            while j < len(sql) and sql[j] != "'":
                j += 1
            tokens.append(Token(TokenType.STRING, sql[i + 1:j]))
            i = j + 1; continue
        # Number
        if ch.isdigit() or (ch == '-' and i + 1 < len(sql) and sql[i + 1].isdigit()):
            j = i + 1 if ch == '-' else i
            while j < len(sql) and sql[j].isdigit():
                j += 1
            if j < len(sql) and sql[j] == '.':
                j += 1
                while j < len(sql) and sql[j].isdigit():
                    j += 1
                tokens.append(Token(TokenType.FLOAT_LIT, sql[i:j]))
            else:
                tokens.append(Token(TokenType.NUMBER, sql[i:j]))
            i = j; continue
        # Identifiers / keywords
        if ch.isalpha() or ch == '_':
            j = i
            while j < len(sql) and (sql[j].isalnum() or sql[j] == '_'):
                j += 1
            word = sql[i:j]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token(KEYWORDS[upper], word))
            elif upper in ("TRUE", "FALSE"):
                tokens.append(Token(TokenType.NUMBER, "1" if upper == "TRUE" else "0"))
            elif upper == "NULL":
                tokens.append(Token(TokenType.STRING, "__NULL__"))
            else:
                tokens.append(Token(TokenType.IDENT, word))
            i = j; continue
        # Symbols
        if ch == '*': tokens.append(Token(TokenType.STAR, ch)); i += 1; continue
        if ch == ',': tokens.append(Token(TokenType.COMMA, ch)); i += 1; continue
        if ch == '(': tokens.append(Token(TokenType.LPAREN, ch)); i += 1; continue
        if ch == ')': tokens.append(Token(TokenType.RPAREN, ch)); i += 1; continue
        if ch == ';': tokens.append(Token(TokenType.SEMICOLON, ch)); i += 1; continue
        if ch == '=': tokens.append(Token(TokenType.EQ, ch)); i += 1; continue
        if ch == '<':
            if i + 1 < len(sql) and sql[i + 1] == '=':
                tokens.append(Token(TokenType.LTE, "<=")); i += 2
            elif i + 1 < len(sql) and sql[i + 1] == '>':
                tokens.append(Token(TokenType.NEQ, "<>")); i += 2
            else:
                tokens.append(Token(TokenType.LT, "<")); i += 1
            continue
        if ch == '>':
            if i + 1 < len(sql) and sql[i + 1] == '=':
                tokens.append(Token(TokenType.GTE, ">=")); i += 2
            else:
                tokens.append(Token(TokenType.GT, ">")); i += 1
            continue
        if ch == '!':
            if i + 1 < len(sql) and sql[i + 1] == '=':
                tokens.append(Token(TokenType.NEQ, "!=")); i += 2; continue
        # Skip unknown chars
        i += 1
    tokens.append(Token(TokenType.EOF, ""))
    return tokens


# -- 12b: AST nodes --------------------------------------------------------

@dataclass
class CreateTableStmt:
    table_name: str
    columns: List[TypingTuple[str, ColumnType, int]]  # (name, type, max_length)

@dataclass
class CreateIndexStmt:
    index_name: str
    table_name: str
    column_name: str

@dataclass
class InsertStmt:
    table_name: str
    values: List[Any]

@dataclass
class SelectStmt:
    table_name: str
    columns: List[str]  # ["*"] for all
    where: Optional[Any] = None  # (col, op, value)

@dataclass
class DeleteStmt:
    table_name: str
    where: Optional[Any] = None

@dataclass
class UpdateStmt:
    table_name: str
    assignments: List[TypingTuple[str, Any]]  # [(col, value), ...]
    where: Optional[Any] = None

@dataclass
class BeginStmt:
    pass

@dataclass
class CommitStmt:
    pass

@dataclass
class RollbackStmt:
    pass


# -- 12c: Parser (recursive descent) --------------------------------------

class Parser:
    """Recursive descent parser for a SQL subset."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> Token:
        return self.tokens[self.pos]

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, tt: TokenType) -> Token:
        tok = self._advance()
        if tok.type != tt:
            raise SyntaxError(f"Expected {tt}, got {tok.type} ('{tok.value}')")
        return tok

    def parse(self):
        tok = self._peek()
        if tok.type == TokenType.CREATE:
            return self._parse_create()
        elif tok.type == TokenType.INSERT:
            return self._parse_insert()
        elif tok.type == TokenType.SELECT:
            return self._parse_select()
        elif tok.type == TokenType.DELETE:
            return self._parse_delete()
        elif tok.type == TokenType.UPDATE_KW:
            return self._parse_update()
        elif tok.type == TokenType.BEGIN_KW:
            self._advance()
            if self._peek().type == TokenType.SEMICOLON:
                self._advance()
            return BeginStmt()
        elif tok.type == TokenType.COMMIT_KW:
            self._advance()
            if self._peek().type == TokenType.SEMICOLON:
                self._advance()
            return CommitStmt()
        elif tok.type == TokenType.ROLLBACK:
            self._advance()
            if self._peek().type == TokenType.SEMICOLON:
                self._advance()
            return RollbackStmt()
        else:
            raise SyntaxError(f"Unexpected token: {tok.type} ('{tok.value}')")

    def _parse_create(self):
        self._expect(TokenType.CREATE)
        tok = self._peek()
        if tok.type == TokenType.TABLE:
            return self._parse_create_table()
        elif tok.type == TokenType.INDEX:
            return self._parse_create_index()
        else:
            raise SyntaxError(f"Expected TABLE or INDEX after CREATE, got {tok.value}")

    def _parse_create_table(self):
        self._expect(TokenType.TABLE)
        name = self._expect(TokenType.IDENT).value
        self._expect(TokenType.LPAREN)
        columns = []
        while True:
            col_name = self._expect(TokenType.IDENT).value
            col_type, max_len = self._parse_column_type()
            columns.append((col_name, col_type, max_len))
            if self._peek().type == TokenType.COMMA:
                self._advance()
            else:
                break
        self._expect(TokenType.RPAREN)
        if self._peek().type == TokenType.SEMICOLON:
            self._advance()
        return CreateTableStmt(name, columns)

    def _parse_column_type(self) -> TypingTuple[ColumnType, int]:
        tok = self._advance()
        if tok.type == TokenType.INTEGER_KW:
            return ColumnType.INTEGER, 0
        elif tok.type == TokenType.FLOAT_KW:
            return ColumnType.FLOAT, 0
        elif tok.type == TokenType.BOOLEAN_KW:
            return ColumnType.BOOLEAN, 0
        elif tok.type == TokenType.VARCHAR_KW:
            max_len = 255
            if self._peek().type == TokenType.LPAREN:
                self._advance()
                max_len = int(self._expect(TokenType.NUMBER).value)
                self._expect(TokenType.RPAREN)
            return ColumnType.VARCHAR, max_len
        else:
            raise SyntaxError(f"Unknown column type: {tok.value}")

    def _parse_create_index(self):
        self._expect(TokenType.INDEX)
        idx_name = self._expect(TokenType.IDENT).value
        self._expect(TokenType.ON)
        tbl_name = self._expect(TokenType.IDENT).value
        self._expect(TokenType.LPAREN)
        col_name = self._expect(TokenType.IDENT).value
        self._expect(TokenType.RPAREN)
        if self._peek().type == TokenType.SEMICOLON:
            self._advance()
        return CreateIndexStmt(idx_name, tbl_name, col_name)

    def _parse_insert(self):
        self._expect(TokenType.INSERT)
        self._expect(TokenType.INTO)
        name = self._expect(TokenType.IDENT).value
        self._expect(TokenType.VALUES)
        self._expect(TokenType.LPAREN)
        values = self._parse_value_list()
        self._expect(TokenType.RPAREN)
        if self._peek().type == TokenType.SEMICOLON:
            self._advance()
        return InsertStmt(name, values)

    def _parse_value_list(self) -> List[Any]:
        values: List[Any] = []
        while True:
            tok = self._peek()
            if tok.type == TokenType.NUMBER:
                self._advance()
                values.append(int(tok.value))
            elif tok.type == TokenType.FLOAT_LIT:
                self._advance()
                values.append(float(tok.value))
            elif tok.type == TokenType.STRING:
                self._advance()
                if tok.value == "__NULL__":
                    values.append(None)
                else:
                    values.append(tok.value)
            else:
                break
            if self._peek().type == TokenType.COMMA:
                self._advance()
            else:
                break
        return values

    def _parse_select(self):
        self._expect(TokenType.SELECT)
        columns: List[str] = []
        if self._peek().type == TokenType.STAR:
            self._advance()
            columns = ["*"]
        else:
            while True:
                columns.append(self._expect(TokenType.IDENT).value)
                if self._peek().type == TokenType.COMMA:
                    self._advance()
                else:
                    break
        self._expect(TokenType.FROM)
        table_name = self._expect(TokenType.IDENT).value
        where = None
        if self._peek().type == TokenType.WHERE:
            self._advance()
            where = self._parse_condition()
        if self._peek().type == TokenType.SEMICOLON:
            self._advance()
        return SelectStmt(table_name, columns, where)

    def _parse_condition(self):
        """Parse: col op value  (supports AND chaining)."""
        left = self._parse_single_condition()
        while self._peek().type == TokenType.AND:
            self._advance()
            right = self._parse_single_condition()
            left = ("AND", left, right)
        return left

    def _parse_single_condition(self):
        col = self._expect(TokenType.IDENT).value
        op_tok = self._advance()
        op_map = {
            TokenType.EQ: "=", TokenType.NEQ: "!=",
            TokenType.LT: "<", TokenType.GT: ">",
            TokenType.LTE: "<=", TokenType.GTE: ">=",
        }
        op = op_map.get(op_tok.type)
        if op is None:
            raise SyntaxError(f"Expected comparison operator, got {op_tok.value}")
        val_tok = self._advance()
        if val_tok.type == TokenType.NUMBER:
            value = int(val_tok.value)
        elif val_tok.type == TokenType.FLOAT_LIT:
            value = float(val_tok.value)
        elif val_tok.type == TokenType.STRING:
            value = val_tok.value
        else:
            raise SyntaxError(f"Expected value, got {val_tok.value}")
        return (col, op, value)

    def _parse_delete(self):
        self._expect(TokenType.DELETE)
        self._expect(TokenType.FROM)
        table_name = self._expect(TokenType.IDENT).value
        where = None
        if self._peek().type == TokenType.WHERE:
            self._advance()
            where = self._parse_condition()
        if self._peek().type == TokenType.SEMICOLON:
            self._advance()
        return DeleteStmt(table_name, where)

    def _parse_update(self):
        self._expect(TokenType.UPDATE_KW)
        table_name = self._expect(TokenType.IDENT).value
        self._expect(TokenType.SET_KW)
        assignments = []
        while True:
            col = self._expect(TokenType.IDENT).value
            self._expect(TokenType.EQ)
            val_tok = self._advance()
            if val_tok.type == TokenType.NUMBER:
                val = int(val_tok.value)
            elif val_tok.type == TokenType.FLOAT_LIT:
                val = float(val_tok.value)
            elif val_tok.type == TokenType.STRING:
                val = val_tok.value
            else:
                raise SyntaxError(f"Expected value in SET, got {val_tok.value}")
            assignments.append((col, val))
            if self._peek().type == TokenType.COMMA:
                self._advance()
            else:
                break
        where = None
        if self._peek().type == TokenType.WHERE:
            self._advance()
            where = self._parse_condition()
        if self._peek().type == TokenType.SEMICOLON:
            self._advance()
        return UpdateStmt(table_name, assignments, where)


# -- 12d: Executor (Volcano iterator model) --------------------------------
# Each operator is an iterator that produces rows one at a time via next().
# This "pull" model is how most real query engines work.

class Operator:
    """Base class for Volcano-style iterators."""
    def init(self): pass
    def next(self) -> Optional[dict]: return None
    def close(self): pass


class SeqScanOp(Operator):
    """Scan all tuples in a heap file, applying MVCC visibility."""

    def __init__(self, heap: HeapFile, schema: Schema,
                 txn_mgr: TransactionManager, txn_id: int):
        self.heap = heap
        self.schema = schema
        self.txn_mgr = txn_mgr
        self.txn_id = txn_id
        self._iter: Optional[Iterator] = None

    def init(self):
        self._iter = self.heap.full_scan()

    def next(self) -> Optional[dict]:
        if self._iter is None:
            return None
        for pid, sid, raw in self._iter:
            row = deserialize_tuple(raw, self.schema)
            row["__pid"] = pid
            row["__sid"] = sid
            xmin = row.get("__xmin", 0)
            xmax = row.get("__xmax", 0)
            if self.txn_mgr.is_visible(xmin, xmax, self.txn_id):
                return row
        return None


class IndexScanOp(Operator):
    """Use a B+Tree index for exact key lookup."""

    def __init__(self, index: BPlusTree, heap: HeapFile, schema: Schema,
                 txn_mgr: TransactionManager, txn_id: int, key: int):
        self.index = index
        self.heap = heap
        self.schema = schema
        self.txn_mgr = txn_mgr
        self.txn_id = txn_id
        self.key = key
        self._done = False

    def init(self):
        self._done = False

    def next(self) -> Optional[dict]:
        if self._done:
            return None
        self._done = True
        result = self.index.search(self.key)
        if result is None:
            return None
        pid, sid = result
        raw = self.heap.get(pid, sid)
        if raw is None:
            return None
        row = deserialize_tuple(raw, self.schema)
        row["__pid"] = pid
        row["__sid"] = sid
        xmin = row.get("__xmin", 0)
        xmax = row.get("__xmax", 0)
        if self.txn_mgr.is_visible(xmin, xmax, self.txn_id):
            return row
        return None


class FilterOp(Operator):
    """Apply a WHERE predicate to rows from a child operator."""

    def __init__(self, child: Operator, condition):
        self.child = child
        self.condition = condition

    def init(self):
        self.child.init()

    def _eval(self, row: dict, cond) -> bool:
        if cond is None:
            return True
        if cond[0] == "AND":
            return self._eval(row, cond[1]) and self._eval(row, cond[2])
        col, op, val = cond
        rv = row.get(col)
        if rv is None:
            return False
        if op == "=":  return rv == val
        if op == "!=": return rv != val
        if op == "<":  return rv < val
        if op == ">":  return rv > val
        if op == "<=": return rv <= val
        if op == ">=": return rv >= val
        return False

    def next(self) -> Optional[dict]:
        while True:
            row = self.child.next()
            if row is None:
                return None
            if self._eval(row, self.condition):
                return row

    def close(self):
        self.child.close()


class ProjectOp(Operator):
    """Select specific columns from each row."""

    def __init__(self, child: Operator, columns: List[str]):
        self.child = child
        self.columns = columns

    def init(self):
        self.child.init()

    def next(self) -> Optional[dict]:
        row = self.child.next()
        if row is None:
            return None
        if self.columns == ["*"]:
            # Strip internal metadata
            return {k: v for k, v in row.items() if not k.startswith("__")}
        return {c: row.get(c) for c in self.columns}

    def close(self):
        self.child.close()


# ============================================================================
# Layer 13: SimpleDB — Top-Level API
# ============================================================================
# This class ties every layer together into a usable database.  You hand it
# a SQL string and it lexes, parses, and executes it, returning rows.

@dataclass
class TableInfo:
    """Catalog entry for a table."""
    name: str
    schema: Schema
    heap: HeapFile
    indexes: Dict[str, BPlusTree] = field(default_factory=dict)
    # Which column each index is on
    index_columns: Dict[str, str] = field(default_factory=dict)


class SimpleDB:
    """
    A tiny but complete relational database.

    Usage:
        db = SimpleDB("/tmp/mydb")
        db.execute("CREATE TABLE users (id INTEGER, name VARCHAR(50), age INTEGER)")
        db.execute("INSERT INTO users VALUES (1, 'Alice', 30)")
        rows = db.execute("SELECT * FROM users WHERE id = 1")
    """

    def __init__(self, db_dir: str, pool_size: int = 64):
        self.db_dir = db_dir
        os.makedirs(db_dir, exist_ok=True)
        self.disk = DiskManager(os.path.join(db_dir, "data.db"))
        self.pool = BufferPool(self.disk, pool_size)
        self.wal = WALManager(os.path.join(db_dir, "wal.log"))
        self.txn_mgr = TransactionManager(self.wal)
        self.tables: Dict[str, TableInfo] = {}
        self._current_txn: Optional[int] = None
        # Reserve page 0 as a header page
        if self.disk.num_pages == 0:
            self.disk.allocate_page()

    def _get_txn(self) -> int:
        """Get current txn, or auto-begin one."""
        if self._current_txn is not None:
            return self._current_txn
        return self.txn_mgr.begin()

    def _auto_commit(self, txn_id: int):
        """Commit if we auto-began this txn (no explicit BEGIN)."""
        if self._current_txn is None:
            self.txn_mgr.commit(txn_id)

    def execute(self, sql: str) -> List[dict]:
        """Execute a SQL statement and return result rows."""
        tokens = lex(sql)
        parser = Parser(tokens)
        stmt = parser.parse()

        if isinstance(stmt, BeginStmt):
            self._current_txn = self.txn_mgr.begin()
            return [{"status": f"BEGIN (txn {self._current_txn})"}]

        if isinstance(stmt, CommitStmt):
            if self._current_txn is not None:
                self.txn_mgr.commit(self._current_txn)
                tid = self._current_txn
                self._current_txn = None
                return [{"status": f"COMMIT (txn {tid})"}]
            return [{"status": "no active transaction"}]

        if isinstance(stmt, RollbackStmt):
            if self._current_txn is not None:
                self.txn_mgr.abort(self._current_txn)
                tid = self._current_txn
                self._current_txn = None
                return [{"status": f"ROLLBACK (txn {tid})"}]
            return [{"status": "no active transaction"}]

        if isinstance(stmt, CreateTableStmt):
            return self._exec_create_table(stmt)
        if isinstance(stmt, CreateIndexStmt):
            return self._exec_create_index(stmt)
        if isinstance(stmt, InsertStmt):
            return self._exec_insert(stmt)
        if isinstance(stmt, SelectStmt):
            return self._exec_select(stmt)
        if isinstance(stmt, DeleteStmt):
            return self._exec_delete(stmt)
        if isinstance(stmt, UpdateStmt):
            return self._exec_update(stmt)

        raise RuntimeError(f"Unknown statement type: {type(stmt)}")

    def _exec_create_table(self, stmt: CreateTableStmt) -> List[dict]:
        cols = []
        for name, ctype, max_len in stmt.columns:
            cols.append(Column(name, ctype, max_length=max_len))
        schema = Schema(cols)
        heap = HeapFile(self.pool)
        self.tables[stmt.table_name] = TableInfo(stmt.table_name, schema, heap)
        return [{"status": f"Created table '{stmt.table_name}'"}]

    def _exec_create_index(self, stmt: CreateIndexStmt) -> List[dict]:
        tinfo = self.tables.get(stmt.table_name)
        if tinfo is None:
            raise RuntimeError(f"Table '{stmt.table_name}' does not exist")
        idx = BPlusTree(self.pool, order=32)
        tinfo.indexes[stmt.index_name] = idx
        tinfo.index_columns[stmt.index_name] = stmt.column_name
        # Back-fill existing data into the index
        txn_id = self._get_txn()
        for pid, sid, raw in tinfo.heap.full_scan():
            row = deserialize_tuple(raw, tinfo.schema)
            key = row.get(stmt.column_name)
            if key is not None and isinstance(key, int):
                idx.insert(key, (pid, sid))
        self._auto_commit(txn_id)
        return [{"status": f"Created index '{stmt.index_name}' on {stmt.table_name}({stmt.column_name})"}]

    def _exec_insert(self, stmt: InsertStmt) -> List[dict]:
        tinfo = self.tables.get(stmt.table_name)
        if tinfo is None:
            raise RuntimeError(f"Table '{stmt.table_name}' does not exist")
        txn_id = self._get_txn()
        data = serialize_tuple(stmt.values, tinfo.schema, xmin=txn_id, xmax=0)
        # WAL first, then data
        self.txn_mgr.log_write(txn_id, WALRecordType.INSERT, 0, data)
        pid, sid = tinfo.heap.insert(data)
        # Update indexes
        for idx_name, idx in tinfo.indexes.items():
            col_name = tinfo.index_columns[idx_name]
            ci = tinfo.schema.col_index(col_name)
            key = stmt.values[ci]
            if key is not None and isinstance(key, int):
                idx.insert(key, (pid, sid))
        self._auto_commit(txn_id)
        return [{"status": f"Inserted 1 row into '{stmt.table_name}' at ({pid},{sid})"}]

    def _exec_select(self, stmt: SelectStmt) -> List[dict]:
        tinfo = self.tables.get(stmt.table_name)
        if tinfo is None:
            raise RuntimeError(f"Table '{stmt.table_name}' does not exist")
        txn_id = self._get_txn()

        # Try to use an index scan if WHERE is col = <int> and we have an index
        scan: Operator
        use_index = False
        if (stmt.where and isinstance(stmt.where, tuple)
                and len(stmt.where) == 3 and stmt.where[1] == "="
                and isinstance(stmt.where[2], int)):
            col_name = stmt.where[0]
            for idx_name, idx_col in tinfo.index_columns.items():
                if idx_col == col_name:
                    idx = tinfo.indexes[idx_name]
                    scan = IndexScanOp(idx, tinfo.heap, tinfo.schema,
                                       self.txn_mgr, txn_id, stmt.where[2])
                    use_index = True
                    break

        if not use_index:
            scan = SeqScanOp(tinfo.heap, tinfo.schema, self.txn_mgr, txn_id)

        # Apply filter (if not fully handled by index)
        if stmt.where and not use_index:
            filtered: Operator = FilterOp(scan, stmt.where)
        else:
            filtered = scan

        projected = ProjectOp(filtered, stmt.columns)
        projected.init()

        rows: List[dict] = []
        while True:
            row = projected.next()
            if row is None:
                break
            rows.append(row)
        projected.close()
        self._auto_commit(txn_id)
        return rows

    def _exec_delete(self, stmt: DeleteStmt) -> List[dict]:
        tinfo = self.tables.get(stmt.table_name)
        if tinfo is None:
            raise RuntimeError(f"Table '{stmt.table_name}' does not exist")
        txn_id = self._get_txn()
        scan = SeqScanOp(tinfo.heap, tinfo.schema, self.txn_mgr, txn_id)
        if stmt.where:
            scan_op: Operator = FilterOp(scan, stmt.where)
        else:
            scan_op = scan
        scan_op.init()
        deleted = 0
        while True:
            row = scan_op.next()
            if row is None:
                break
            pid, sid = row["__pid"], row["__sid"]
            # Mark tuple with xmax = txn_id (MVCC delete)
            raw = tinfo.heap.get(pid, sid)
            if raw is not None:
                # Rewrite tuple with xmax set
                new_raw = bytearray(raw)
                new_raw[4:8] = pack_u32(txn_id)
                page = self.pool.fetch_page(pid)
                off, length = page.line_pointers[sid]
                page.raw[off:off + length] = new_raw
                self.pool.unpin(pid, dirty=True)
                self.txn_mgr.log_write(txn_id, WALRecordType.DELETE, pid)
            deleted += 1
        self._auto_commit(txn_id)
        return [{"status": f"Deleted {deleted} row(s) from '{stmt.table_name}'"}]

    def _exec_update(self, stmt: UpdateStmt) -> List[dict]:
        tinfo = self.tables.get(stmt.table_name)
        if tinfo is None:
            raise RuntimeError(f"Table '{stmt.table_name}' does not exist")
        txn_id = self._get_txn()
        scan = SeqScanOp(tinfo.heap, tinfo.schema, self.txn_mgr, txn_id)
        if stmt.where:
            scan_op: Operator = FilterOp(scan, stmt.where)
        else:
            scan_op = scan
        scan_op.init()
        updated = 0
        while True:
            row = scan_op.next()
            if row is None:
                break
            pid, sid = row["__pid"], row["__sid"]
            # Mark old tuple deleted (set xmax)
            raw = tinfo.heap.get(pid, sid)
            if raw is None:
                continue
            new_raw = bytearray(raw)
            new_raw[4:8] = pack_u32(txn_id)
            page = self.pool.fetch_page(pid)
            off, length = page.line_pointers[sid]
            page.raw[off:off + length] = new_raw
            self.pool.unpin(pid, dirty=True)
            # Insert new version
            values = []
            for col in tinfo.schema.columns:
                values.append(row.get(col.name))
            for col_name, val in stmt.assignments:
                ci = tinfo.schema.col_index(col_name)
                values[ci] = val
            new_data = serialize_tuple(values, tinfo.schema, xmin=txn_id, xmax=0)
            self.txn_mgr.log_write(txn_id, WALRecordType.UPDATE, pid, new_data)
            new_pid, new_sid = tinfo.heap.insert(new_data)
            # Update indexes
            for idx_name, idx in tinfo.indexes.items():
                col_name = tinfo.index_columns[idx_name]
                ci = tinfo.schema.col_index(col_name)
                key = values[ci]
                if key is not None and isinstance(key, int):
                    idx.insert(key, (new_pid, new_sid))
            updated += 1
        self._auto_commit(txn_id)
        return [{"status": f"Updated {updated} row(s) in '{stmt.table_name}'"}]

    def flush(self):
        """Flush all dirty pages and WAL to disk."""
        self.pool.flush_all()
        self.wal.flush()

    def close(self):
        self.flush()
        self.disk.close()
        self.wal.close()


# ============================================================================
# Layer 14: Demo / Main — interactive walkthrough
# ============================================================================

def _banner(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def demo():
    """Run a full walkthrough of every database layer."""
    db_dir = tempfile.mkdtemp(prefix="simpledb_")
    print(f"SimpleDB — A Database from Scratch")
    print(f"Working directory: {db_dir}")

    # ------------------------------------------------------------------
    # Demo 1: Page-level operations and checksums
    # ------------------------------------------------------------------
    _banner("Layer 2-3: DiskManager + SlottedPage")
    disk = DiskManager(os.path.join(db_dir, "demo_pages.db"))
    pid = disk.allocate_page()
    print(f"  Allocated page {pid} (offset {pid * PAGE_SIZE} in file)")

    page = SlottedPage(page_id=pid)
    s0 = page.add_tuple(b"Hello, database world!")
    s1 = page.add_tuple(b"Second tuple here")
    print(f"  Added tuple 0 at slot {s0}: {page.get_tuple(s0)}")
    print(f"  Added tuple 1 at slot {s1}: {page.get_tuple(s1)}")
    print(f"  Free space remaining: {page.free_space()} bytes")

    data = page.to_bytes()
    cs = unpack_u32(data[12:16])
    print(f"  Page checksum (CRC-32): 0x{cs:08X}")
    print(f"  Checksum valid: {page.verify_checksum()}")

    page.delete_tuple(s0)
    print(f"  Deleted slot {s0}, then compacting...")
    page.compact()
    print(f"  After compaction, free space: {page.free_space()} bytes")

    disk.write_page(pid, page.to_bytes())
    disk.fsync()
    print(f"  Page written to disk and fsynced")
    disk.close()

    # ------------------------------------------------------------------
    # Demo 2: Buffer Pool with clock replacement
    # ------------------------------------------------------------------
    _banner("Layer 4: BufferPool (Clock Replacement)")
    disk2 = DiskManager(os.path.join(db_dir, "demo_pool.db"))
    pool = BufferPool(disk2, pool_size=4)
    # Allocate more pages than the pool can hold
    pids = []
    for i in range(6):
        p = disk2.allocate_page()
        pids.append(p)
        # Initialise each page on disk
        sp = SlottedPage(page_id=p)
        sp.add_tuple(f"page-{p}-data".encode())
        disk2.write_page(p, sp.to_bytes())

    for p in pids:
        pg = pool.fetch_page(p)
        pool.unpin(p)
    print(f"  Fetched {len(pids)} pages through a 4-frame pool")
    print(f"  Cache hits: {pool.hits}, misses: {pool.misses}")
    # Re-fetch a recent page — should be a hit
    pg = pool.fetch_page(pids[-1])
    pool.unpin(pids[-1])
    print(f"  After re-fetching page {pids[-1]}: hits={pool.hits}, misses={pool.misses}")
    disk2.close()

    # ------------------------------------------------------------------
    # Demo 3: WAL writes and simulated crash recovery
    # ------------------------------------------------------------------
    _banner("Layer 5: Write-Ahead Log (WAL)")
    wal = WALManager(os.path.join(db_dir, "demo.wal"))
    lsn1 = wal.append(1, WALRecordType.BEGIN, 0)
    lsn2 = wal.append(1, WALRecordType.INSERT, 10, b"row_data_here")
    lsn3 = wal.append(1, WALRecordType.COMMIT, 0)
    lsn4 = wal.append(2, WALRecordType.BEGIN, 0)
    lsn5 = wal.append(2, WALRecordType.INSERT, 11, b"another_row")
    # Txn 2 never commits — simulates a crash
    wal.flush()
    print(f"  Wrote 5 WAL records (LSNs {lsn1}-{lsn5})")
    print(f"  Txn 1: BEGIN -> INSERT -> COMMIT")
    print(f"  Txn 2: BEGIN -> INSERT -> (crash — no COMMIT)")

    records = wal.recover()
    began = {r.txn_id for r in records if r.record_type == WALRecordType.BEGIN}
    committed = {r.txn_id for r in records if r.record_type == WALRecordType.COMMIT}
    print(f"  Recovery: {len(records)} records replayed")
    print(f"  Transactions that began:    {began}")
    print(f"  Transactions that committed: {committed}")
    print(f"  Must abort (began - committed): {began - committed}")
    wal.close()

    # ------------------------------------------------------------------
    # Demo 4: Heap File insert/scan
    # ------------------------------------------------------------------
    _banner("Layer 6-7: HeapFile + Tuple Serialization")
    disk3 = DiskManager(os.path.join(db_dir, "demo_heap.db"))
    pool3 = BufferPool(disk3, pool_size=16)
    heap = HeapFile(pool3)
    schema = Schema([
        Column("id", ColumnType.INTEGER),
        Column("name", ColumnType.VARCHAR, max_length=50),
        Column("score", ColumnType.FLOAT),
    ])
    names = ["Alice", "Bob", "Charlie", "Diana", "Eve"]
    for i, name in enumerate(names):
        data = serialize_tuple([i + 1, name, (i + 1) * 10.5], schema, xmin=1, xmax=0)
        pid_, sid_ = heap.insert(data)
        print(f"  Inserted ({i+1}, '{name}', {(i+1)*10.5:.1f}) at page={pid_} slot={sid_}")

    print("  Full scan:")
    for pid_, sid_, raw_ in heap.full_scan():
        row = deserialize_tuple(raw_, schema)
        print(f"    [{pid_}:{sid_}] id={row['id']} name={row['name']} score={row['score']}")
    disk3.close()

    # ------------------------------------------------------------------
    # Demo 5: B+Tree index search and range scan
    # ------------------------------------------------------------------
    _banner("Layer 8: B+Tree Index")
    disk4 = DiskManager(os.path.join(db_dir, "demo_btree.db"))
    pool4 = BufferPool(disk4, pool_size=32)
    btree = BPlusTree(pool4, order=4)  # small order to force splits
    print("  Inserting keys 1-20 into B+Tree (order=4)...")
    for k in range(1, 21):
        btree.insert(k, (k, 0))  # rid = (k, 0) for demo

    result = btree.search(7)
    print(f"  search(7) = {result}")
    result = btree.search(99)
    print(f"  search(99) = {result}")
    print("  range_scan(5, 12):")
    for key, rid in btree.range_scan(5, 12):
        print(f"    key={key}, rid={rid}")
    disk4.close()

    # ------------------------------------------------------------------
    # Demo 6: Transaction commit/rollback with MVCC
    # ------------------------------------------------------------------
    _banner("Layer 9: Transactions & MVCC Visibility")
    wal2 = WALManager(os.path.join(db_dir, "demo_mvcc.wal"))
    txn_mgr = TransactionManager(wal2)

    t1 = txn_mgr.begin()
    t2 = txn_mgr.begin()
    print(f"  Started txn {t1} and txn {t2}")

    txn_mgr.commit(t1)
    print(f"  Committed txn {t1}")
    print(f"  Row with xmin={t1}, xmax=0:")
    print(f"    Visible to any reader? {txn_mgr.is_visible(t1, 0)}")
    print(f"  Row with xmin={t2}, xmax=0:")
    print(f"    Visible to t1 (other txn)? {txn_mgr.is_visible(t2, 0, t1)}")
    print(f"    Visible to t2 (own txn)?   {txn_mgr.is_visible(t2, 0, t2)}")

    txn_mgr.abort(t2)
    print(f"  Aborted txn {t2}")
    print(f"  Row with xmin={t2}, xmax=0:")
    print(f"    Visible after abort? {txn_mgr.is_visible(t2, 0)}")
    wal2.close()

    # ------------------------------------------------------------------
    # Demo 7: Bloom Filter
    # ------------------------------------------------------------------
    _banner("Layer 10: Bloom Filter")
    bf = BloomFilter(capacity=100, fp_rate=0.05)
    for w in ["apple", "banana", "cherry", "date", "elderberry"]:
        bf.add(w)
    print(f"  Bloom filter: {bf.size} bits, {bf.k} hash functions")
    for w in ["apple", "cherry", "fig", "grape"]:
        r = bf.might_contain(w)
        print(f"  might_contain('{w}'): {r}")

    # ------------------------------------------------------------------
    # Demo 8: LSM Tree
    # ------------------------------------------------------------------
    _banner("Layer 11: LSM Tree")
    lsm_dir = os.path.join(db_dir, "lsm")
    lsm = LSMTree(lsm_dir, memtable_size=5)
    print("  Inserting 12 key-value pairs (memtable_size=5, triggers flushes)...")
    for i in range(12):
        lsm.put(f"key_{i:03d}", f"value_{i}".encode())
    print(f"  SSTables on disk: {len(lsm.sstables)}")
    for sst in lsm.sstables:
        print(f"    {os.path.basename(sst.filepath)}: {len(sst.index)} entries")

    val = lsm.get("key_007")
    print(f"  get('key_007') = {val}")
    lsm.delete("key_003")
    val = lsm.get("key_003")
    print(f"  After delete, get('key_003') = {val}")

    # ------------------------------------------------------------------
    # Demo 9: Full SQL Query Engine
    # ------------------------------------------------------------------
    _banner("Layer 12-13: SQL Query Engine")
    db = SimpleDB(os.path.join(db_dir, "sqldb"))

    sqls = [
        "CREATE TABLE users (id INTEGER, name VARCHAR(50), age INTEGER);",
        "INSERT INTO users VALUES (1, 'Alice', 30);",
        "INSERT INTO users VALUES (2, 'Bob', 25);",
        "INSERT INTO users VALUES (3, 'Charlie', 35);",
        "INSERT INTO users VALUES (4, 'Diana', 28);",
        "INSERT INTO users VALUES (5, 'Eve', 32);",
        "CREATE INDEX idx_id ON users (id);",
    ]
    for sql in sqls:
        result = db.execute(sql)
        print(f"  SQL> {sql}")
        for r in result:
            print(f"       -> {r}")

    print()
    queries = [
        "SELECT * FROM users;",
        "SELECT name, age FROM users WHERE age > 28;",
        "SELECT * FROM users WHERE id = 3;",
    ]
    for q in queries:
        print(f"  SQL> {q}")
        rows = db.execute(q)
        for r in rows:
            print(f"       {r}")
        print()

    # Transaction demo
    print("  -- Transaction demo --")
    db.execute("BEGIN;")
    db.execute("INSERT INTO users VALUES (6, 'Frank', 40);")
    mid_rows = db.execute("SELECT * FROM users WHERE id = 6;")
    print(f"  Within txn, SELECT id=6: {mid_rows}")
    db.execute("ROLLBACK;")
    after_rows = db.execute("SELECT * FROM users WHERE id = 6;")
    print(f"  After ROLLBACK, SELECT id=6: {after_rows}")

    # Delete demo
    print()
    db.execute("DELETE FROM users WHERE id = 5;")
    print("  SQL> DELETE FROM users WHERE id = 5;")
    rows = db.execute("SELECT * FROM users;")
    print(f"  Remaining rows: {len(rows)}")
    for r in rows:
        print(f"       {r}")

    # Update demo
    print()
    db.execute("UPDATE users SET age = 31 WHERE id = 1;")
    print("  SQL> UPDATE users SET age = 31 WHERE id = 1;")
    rows = db.execute("SELECT * FROM users WHERE id = 1;")
    for r in rows:
        print(f"       {r}")

    db.close()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    _banner("Summary")
    print("""  You've just seen a database built from first principles:

    1. DiskManager     - Raw OS I/O, page-aligned reads/writes
    2. SlottedPage     - Page layout with line pointers and checksums
    3. BufferPool      - Clock replacement algorithm for page caching
    4. WAL             - Write-ahead logging for crash recovery
    5. HeapFile        - Unordered tuple storage with free-space map
    6. Tuple/Schema    - Row serialization with null bitmaps & MVCC headers
    7. B+Tree          - Ordered index with search, insert, splits, range scans
    8. Transactions    - MVCC visibility rules (read committed)
    9. Bloom Filter    - Probabilistic membership test
   10. LSM Tree        - Write-optimised storage with SSTables & compaction
   11. Query Engine    - SQL lexer, parser, and Volcano-style executor

  Every concept maps directly to what PostgreSQL, MySQL, RocksDB,
  and other production databases do internally.
""")

    # Cleanup
    shutil.rmtree(db_dir, ignore_errors=True)
    print(f"  Cleaned up {db_dir}")


if __name__ == "__main__":
    demo()
