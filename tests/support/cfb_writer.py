"""Author a minimal OLE2 / Compound File Binary container for tests.

``olefile`` reads CFB but cannot write it, and there is no Outlook to hand, so
without this the ``.msg`` reader could only be tested against a mock of the
format -- which is exactly the kind of test that passes while the real parser
returns nothing. This produces genuine containers that ``olefile`` opens.

Deliberately restricted to what the tests need:

* version 3 (512-byte sectors), no DIFAT sectors, so at most 109 FAT sectors --
  about 27 MB of payload, far past any fixture.
* streams smaller than 4096 bytes go in the mini-stream, as the format
  requires. Writing a smaller cutoff in the header to avoid implementing it
  does not work: olefile logs "Fixing the mini_stream_cutoff_size to 4096
  (mandatory value)" and reads small streams from the mini-stream regardless,
  so a container without one yields empty strings for every field.
* the directory is a degenerate right-leaning tree rather than a balanced
  red-black one. Readers walk the links rather than re-deriving the ordering.
"""

import struct
from typing import Iterable

SECTOR = 512
MINI_SECTOR = 64
# Mandatory per [MS-CFB]; olefile rewrites any other value in the header.
MINI_CUTOFF = 4096
DIFAT_ENTRIES_IN_HEADER = 109

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD

# Directory entry object types.
_EMPTY, _STORAGE, _STREAM, _ROOT = 0, 1, 2, 5
_NOSTREAM = 0xFFFFFFFF


class _Entry:
    def __init__(self, name: str, kind: int):
        self.name = name
        self.kind = kind
        self.child = _NOSTREAM
        self.right = _NOSTREAM
        self.start = ENDOFCHAIN
        self.size = 0


def _pad(data: bytes, multiple: int = SECTOR) -> bytes:
    remainder = len(data) % multiple
    return data if not remainder else data + b"\x00" * (multiple - remainder)


def _directory_entry(entry: _Entry) -> bytes:
    raw = bytearray(b"\x00" * 128)
    encoded = entry.name.encode("utf-16-le")[:62]
    raw[0 : len(encoded)] = encoded
    struct.pack_into("<H", raw, 0x40, len(encoded) + 2)
    raw[0x42] = entry.kind
    raw[0x43] = 1  # black
    struct.pack_into("<I", raw, 0x44, _NOSTREAM)  # left sibling
    struct.pack_into("<I", raw, 0x48, entry.right)
    struct.pack_into("<I", raw, 0x4C, entry.child)
    struct.pack_into("<I", raw, 0x74, entry.start)
    struct.pack_into("<Q", raw, 0x78, entry.size)
    return bytes(raw)


def _link_siblings(entries: list[_Entry], indices: list[int]) -> int:
    """Chain ``indices`` as right siblings; return the first, or _NOSTREAM."""
    if not indices:
        return _NOSTREAM
    for current, following in zip(indices, indices[1:]):
        entries[current].right = following
    return indices[0]


def write_cfb(streams: dict[str, bytes]) -> bytes:
    """Build a CFB container holding ``streams``.

    Keys are paths; a single ``/`` introduces one level of storage, which is all
    a ``.msg``'s attachment folders need (``__attach_version1.0_#00000000/...``).
    """
    root = _Entry("Root Entry", _ROOT)
    entries: list[_Entry] = [root]

    # Regular payload and mini payload are accumulated separately: a stream's
    # start sector is an index into whichever of the two holds it, so both must
    # be complete before the directory naming them can be written.
    regular = bytearray()
    mini = bytearray()

    def allocate(data: bytes) -> tuple[int, int]:
        if not data:
            return ENDOFCHAIN, 0
        if len(data) < MINI_CUTOFF:
            start = len(mini) // MINI_SECTOR
            mini.extend(_pad(data, MINI_SECTOR))
            return start, len(data)
        start = len(regular) // SECTOR
        regular.extend(_pad(data))
        return start, len(data)

    top_level: list[int] = []
    storages: dict[str, tuple[_Entry, list[int]]] = {}

    for path, data in streams.items():
        storage_name, _, leaf = path.rpartition("/")
        entry = _Entry(leaf, _STREAM)
        entry.start, entry.size = allocate(data)
        entries.append(entry)
        index = len(entries) - 1
        if not storage_name:
            top_level.append(index)
            continue
        if storage_name not in storages:
            storage_entry = _Entry(storage_name, _STORAGE)
            entries.append(storage_entry)
            top_level.append(len(entries) - 1)
            storages[storage_name] = (storage_entry, [])
        storages[storage_name][1].append(index)

    for storage_entry, children in storages.values():
        storage_entry.child = _link_siblings(entries, children)
    root.child = _link_siblings(entries, top_level)

    # The mini-stream container is itself an ordinary stream, owned by the root
    # entry, laid down after the regular payload.
    mini_container_start = len(regular) // SECTOR if mini else ENDOFCHAIN
    root.start = mini_container_start
    root.size = len(mini)
    regular.extend(_pad(bytes(mini)))

    directory = _pad(b"".join(_directory_entry(e) for e in entries))
    directory_sectors = len(directory) // SECTOR
    payload_sectors = len(regular) // SECTOR

    mini_fat = [FREESECT] * (len(mini) // MINI_SECTOR)
    cursor = 0
    for data in streams.values():
        if not data or len(data) >= MINI_CUTOFF:
            continue
        count = -(-len(data) // MINI_SECTOR)
        for offset in range(count):
            slot = cursor + offset
            mini_fat[slot] = ENDOFCHAIN if offset == count - 1 else slot + 1
        cursor += count
    mini_fat_bytes = _pad(b"".join(struct.pack("<I", v) for v in mini_fat))
    mini_fat_sectors = len(mini_fat_bytes) // SECTOR if mini_fat else 0

    # The FAT must describe itself, so its size is solved by iteration: adding a
    # FAT sector adds entries, which can require another FAT sector.
    fat_sectors = 1
    while True:
        total = payload_sectors + directory_sectors + mini_fat_sectors + fat_sectors
        needed = -(-total // (SECTOR // 4))
        if needed <= fat_sectors:
            break
        fat_sectors = needed
    if fat_sectors > DIFAT_ENTRIES_IN_HEADER:
        raise ValueError("payload too large for a header-only DIFAT")

    directory_start = payload_sectors
    mini_fat_start = directory_start + directory_sectors
    fat_start = mini_fat_start + mini_fat_sectors

    fat = [FREESECT] * (fat_sectors * (SECTOR // 4))

    def chain(start: int, count: int) -> None:
        for offset in range(count):
            sector = start + offset
            fat[sector] = ENDOFCHAIN if offset == count - 1 else sector + 1

    # Every stream is contiguous, so each chain simply runs to its end.
    cursor = 0
    for data in streams.values():
        if not data or len(data) < MINI_CUTOFF:
            continue
        length = len(_pad(data)) // SECTOR
        chain(cursor, length)
        cursor += length
    if mini:
        chain(mini_container_start, len(_pad(bytes(mini))) // SECTOR)
    chain(directory_start, directory_sectors)
    if mini_fat_sectors:
        chain(mini_fat_start, mini_fat_sectors)
    for offset in range(fat_sectors):
        fat[fat_start + offset] = FATSECT

    header = bytearray(b"\x00" * SECTOR)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x18, 0x003E)  # minor version
    struct.pack_into("<H", header, 0x1A, 3)  # major version
    struct.pack_into("<H", header, 0x1C, 0xFFFE)  # little endian
    struct.pack_into("<H", header, 0x1E, 9)  # 512-byte sectors
    struct.pack_into("<H", header, 0x20, 6)  # 64-byte mini sectors
    struct.pack_into("<I", header, 0x2C, fat_sectors)
    struct.pack_into("<I", header, 0x30, directory_start)
    struct.pack_into("<I", header, 0x38, MINI_CUTOFF)
    struct.pack_into(
        "<I", header, 0x3C, mini_fat_start if mini_fat_sectors else ENDOFCHAIN
    )
    struct.pack_into("<I", header, 0x40, mini_fat_sectors)
    struct.pack_into("<I", header, 0x44, ENDOFCHAIN)  # first DIFAT
    for slot in range(DIFAT_ENTRIES_IN_HEADER):
        value = fat_start + slot if slot < fat_sectors else FREESECT
        struct.pack_into("<I", header, 0x4C + slot * 4, value)

    fat_bytes = b"".join(struct.pack("<I", value) for value in fat)
    return bytes(header) + bytes(regular) + directory + mini_fat_bytes + fat_bytes


def cfb_with(streams: Iterable[tuple[str, bytes]]) -> bytes:
    """``write_cfb`` from pairs, for callers building the list incrementally."""
    return write_cfb(dict(streams))
