#!/usr/bin/env python3
"""DoomGeo generic WAD import pipeline.

Provides a unified entry point for importing DOOM WAD maps into the engine's
internal format.  Wraps the existing doom_convert.py / doom_chunk_convert.py /
doom_ripdoom_convert.py converters with:

  - Full WAD structure analysis and validation
  - Geometry integrity checks (missing sectors, inverted walls, etc.)
  - Conversion statistics and timing
  - Engine-native cache files to avoid redundant re-conversion
  - A data-driven, extensible architecture that supports any Doom map

Usage::

    python3 tools/doom_import.py --iwad .tools/assets/doom1.wad.zip --map E1M1
    python3 tools/doom_import.py --iwad .tools/assets/doom1.wad.zip --map E1M1 --validate
    python3 tools/doom_import.py --iwad .tools/assets/doom1.wad.zip --map E1M1 --stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from zipfile import ZipFile


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WAD_IN_ZIP = "freedoom-0.13.0/freedoom1.wad"

PLAYER_HEIGHT = 56
PLAYER_MAX_STEP_HEIGHT = 24

LINEDEF_FLAG_TWO_SIDED = 0x0004
LINEDEF_FLAG_BLOCKING = 0x0001
LINEDEF_FLAG_UPPER_UNPEGGED = 0x0008
LINEDEF_FLAG_LOWER_UNPEGGED = 0x0010

THING_TYPE_PLAYER1 = 1

DOOR_SPECIALS = frozenset({
    1, 26, 27, 28, 31, 32, 33, 34, 46, 61, 63, 76, 86, 90, 103, 117, 118,
})

EXIT_SPECIALS = frozenset({
    11, 51, 52, 124, 197, 198, 243, 244,
})

LIFT_SPECIALS = frozenset({
    62, 88, 89, 120, 121, 122, 123,
})

SECRET_SECTOR_SPECIAL = 9

DAMAGE_SECTOR_SPECIALS = {
    4: 5,    # nukage: 5 damage
    5: 10,   # nukage: 10 damage (unused in shareware)
    7: 5,    # nukage: 5 damage
    9: 20,   # lava: 20 damage
    11: 20,  # lava: 20 damage (instant death variant, but treat as lava)
    16: 20,  # lava: 20 damage
}

MONSTER_TYPES = frozenset({
    3004,  # Former Human
    9,     # Former Sergeant
    65,    # Heavy Weapon Dude (Chaingunner, not in shareware)
    3001,  # Imp
    3002,  # Demon
    58,    # Spectre
    3005,  # Cacodemon
    3006,  # Lost Soul
    3003,  # Baron of Hell
    69,    # Hell Knight (not in shareware)
    68,    # Arachnotron (not in shareware)
    71,    # Pain Elemental (not in shareware)
    66,    # Revenant (not in shareware)
    67,    # Mancubus (not in shareware)
    64,    # Arch-vile (not in shareware)
    16,    # Cyberdemon
    7,     # Spider Mastermind
    84,    # Wolfenstein SS (not in shareware)
})

PICKUP_TYPES = frozenset({
    2001,  # Shotgun
    2002,  # Chaingun
    2003,  # Rocket Launcher
    2004,  # Plasma Rifle
    2006,  # BFG9000
    2005,  # Chainsaw
    5,     # Blue Keycard
    6,     # Yellow Keycard
    40,    # Red Keycard
    13,    # Blue Skull Key
    38,    # Red Skull Key
    39,    # Yellow Skull Key
    2007,  # Clip
    2008,  # 4 Shells
    2010,  # Rocket
    2047,  # Cell
    2046,  # Box of Shells
    2048,  # Box of Bullets
    2049,  # Rocket Box
    17,    # Cell Pack
    8,     # Backpack
    2011,  # Stimpack
    2012,  # Medikit
    2014,  # Health Bonus
    2015,  # Armor Bonus
    2018,  # Green Armor
    2019,  # Blue Armor
    2013,  # Soul Sphere
    2022,  # Invulnerability
    2023,  # Berserk
    2024,  # Invisibility
    2025,  # Radiation Suit
    2026,  # Computer Map
    2045,  # Light Amp Visor
    83,    # Megasphere (not in shareware)
})

BARREL_TYPES = frozenset({2035})



# ---------------------------------------------------------------------------
# WAD Structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Lump:
    name: str
    offset: int
    size: int


@dataclass(frozen=True)
class Vertex:
    x: int
    y: int


@dataclass(frozen=True)
class LineDef:
    v1: int
    v2: int
    flags: int
    special: int
    tag: int
    side_front: int
    side_back: int


@dataclass(frozen=True)
class SideDef:
    texture_x: int
    texture_y: int
    top_texture: str
    bottom_texture: str
    mid_texture: str
    sector: int


@dataclass(frozen=True)
class Sector:
    floor_height: int
    ceiling_height: int
    floor_pic: str
    ceiling_pic: str
    light_level: int
    special: int
    tag: int


@dataclass(frozen=True)
class Seg:
    v1: int
    v2: int
    angle: int
    linedef: int
    side: int
    offset: int


@dataclass(frozen=True)
class Subsector:
    numsegs: int
    firstseg: int


@dataclass(frozen=True)
class Node:
    x: int
    y: int
    dx: int
    dy: int
    bbox: tuple[int, int, int, int, int, int, int, int]
    child0: int
    child1: int


@dataclass(frozen=True)
class Thing:
    x: int
    y: int
    angle: int
    type: int
    flags: int



# ---------------------------------------------------------------------------
# WAD Loader
# ---------------------------------------------------------------------------

class WadLoader:
    """Robust WAD file loader with full lump directory support."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.lumps: list[Lump] = []
        self.by_name: dict[str, list[int]] = {}
        self._parse_header()

    def _parse_header(self) -> None:
        if len(self.data) < 12:
            raise ValueError("WAD file too small (need at least 12 bytes)")
        ident, num_lumps, dir_offset = struct.unpack_from("<4sii", self.data, 0)
        if ident not in (b"IWAD", b"PWAD"):
            raise ValueError(f"not a WAD file: {ident!r}")
        self.is_iwad = ident == b"IWAD"
        if num_lumps < 0 or dir_offset < 0:
            raise ValueError(f"invalid WAD header: num_lumps={num_lumps}, dir_offset={dir_offset}")
        if dir_offset + num_lumps * 16 > len(self.data):
            raise ValueError("WAD directory extends beyond file")
        for i in range(num_lumps):
            off, size, raw_name = struct.unpack_from("<ii8s", self.data, dir_offset + i * 16)
            name = raw_name.rstrip(b"\0").decode("ascii", "replace").upper()
            self.lumps.append(Lump(name, off, size))
            self.by_name.setdefault(name, []).append(i)

    def lump_data(self, index: int) -> bytes:
        lump = self.lumps[index]
        return self.data[lump.offset:lump.offset + lump.size]

    def find_lump(self, name: str) -> int | None:
        indices = self.by_name.get(name.upper())
        return indices[0] if indices else None

    def map_lumps(self, marker: str) -> dict[str, bytes]:
        marker = marker.upper()
        try:
            start = next(i for i, l in enumerate(self.lumps) if l.name == marker)
        except StopIteration as exc:
            raise ValueError(f"map marker {marker!r} not found") from exc

        wanted = {
            "THINGS", "LINEDEFS", "SIDEDEFS", "VERTEXES",
            "SEGS", "SSECTORS", "NODES", "SECTORS", "REJECT", "BLOCKMAP",
        }
        result: dict[str, bytes] = {}
        for i in range(start + 1, min(start + 16, len(self.lumps))):
            name = self.lumps[i].name
            if name in wanted:
                result[name] = self.lump_data(i)
            if len(result) >= len(wanted):
                break
        for required in wanted:
            if required not in result:
                raise ValueError(f"map {marker} missing required lump {required}")
        return result

    def available_maps(self) -> list[str]:
        maps = []
        for lump in self.lumps:
            if lump.name.startswith("E") and lump.name[2:3] == "M" and len(lump.name) == 4:
                maps.append(lump.name)
            elif lump.name.startswith("MAP") and len(lump.name) == 5:
                maps.append(lump.name)
        return sorted(set(maps))



# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def decode_lump_name(raw: bytes) -> str:
    return raw.rstrip(b"\0").decode("ascii", "replace").upper()


def parse_vertices(data: bytes) -> list[Vertex]:
    if len(data) % 4:
        raise ValueError("VERTEXES lump has invalid size")
    return [Vertex(*struct.unpack_from("<hh", data, i)) for i in range(0, len(data), 4)]


def parse_linedefs(data: bytes) -> list[LineDef]:
    if len(data) % 14:
        raise ValueError("LINEDEFS lump has invalid size")
    return [LineDef(*struct.unpack_from("<HHHHHHH", data, i)) for i in range(0, len(data), 14)]


def parse_sidedefs(data: bytes) -> list[SideDef]:
    if len(data) % 30:
        raise ValueError("SIDEDEFS lump has invalid size")
    result: list[SideDef] = []
    for i in range(0, len(data), 30):
        tex_x, tex_y, top, bottom, mid, sector = struct.unpack_from("<hh8s8s8sh", data, i)
        result.append(SideDef(tex_x, tex_y, decode_lump_name(top), decode_lump_name(bottom), decode_lump_name(mid), sector))
    return result


def parse_sectors(data: bytes) -> list[Sector]:
    if len(data) % 26:
        raise ValueError("SECTORS lump has invalid size")
    result: list[Sector] = []
    for i in range(0, len(data), 26):
        floor, ceiling, floor_pic, ceiling_pic, light, special, tag = struct.unpack_from("<hh8s8shhh", data, i)
        result.append(Sector(floor, ceiling, decode_lump_name(floor_pic), decode_lump_name(ceiling_pic), light, special, tag))
    return result


def parse_things(data: bytes) -> list[Thing]:
    if len(data) % 10:
        raise ValueError("THINGS lump has invalid size")
    return [Thing(*struct.unpack_from("<hhHHH", data, i)) for i in range(0, len(data), 10)]


def parse_segs(data: bytes) -> list[Seg]:
    if len(data) % 12:
        raise ValueError("SEGS lump has invalid size")
    return [Seg(*struct.unpack_from("<HHHHHH", data, i)) for i in range(0, len(data), 12)]


def parse_subsectors(data: bytes) -> list[Subsector]:
    if len(data) % 4:
        raise ValueError("SSECTORS lump has invalid size")
    return [Subsector(*struct.unpack_from("<HH", data, i)) for i in range(0, len(data), 4)]


def parse_nodes(data: bytes) -> list[Node]:
    if len(data) % 28:
        raise ValueError("NODES lump has invalid size")
    result: list[Node] = []
    for i in range(0, len(data), 28):
        x, y, dx, dy = struct.unpack_from("<hhhh", data, i)
        bbox = struct.unpack_from("<hhhhhhhh", data, i + 8)
        child0, child1 = struct.unpack_from("<HH", data, i + 24)
        result.append(Node(x, y, dx, dy, bbox, child0, child1))
    return result


def parse_blockmap_words(data: bytes) -> list[int]:
    if len(data) % 2:
        raise ValueError("BLOCKMAP lump has invalid size")
    return [struct.unpack_from("<h", data, i)[0] for i in range(0, len(data), 2)]



# ---------------------------------------------------------------------------
# Import Statistics
# ---------------------------------------------------------------------------

@dataclass
class ImportStats:
    map_name: str = ""
    source_file: str = ""

    # Counts
    vertex_count: int = 0
    linedef_count: int = 0
    sidedef_count: int = 0
    sector_count: int = 0
    seg_count: int = 0
    subsector_count: int = 0
    node_count: int = 0
    thing_count: int = 0
    reject_bytes: int = 0
    blockmap_words: int = 0

    # Derived counts
    solid_linedef_count: int = 0
    two_sided_linedef_count: int = 0
    door_count: int = 0
    exit_count: int = 0
    lift_count: int = 0
    monster_count: int = 0
    pickup_count: int = 0
    barrel_count: int = 0
    player_start_count: int = 0

    # Texture info
    unique_textures: set[str] = field(default_factory=set)
    unique_flats: set[str] = field(default_factory=set)

    # Geometry bounds
    min_x: int = 0
    max_x: int = 0
    min_y: int = 0
    max_y: int = 0
    width_doom_units: int = 0
    height_doom_units: int = 0

    # Light levels
    min_light: int = 255
    max_light: int = 0

    # Validation
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Timing
    parse_time_ms: float = 0.0
    validate_time_ms: float = 0.0
    convert_time_ms: float = 0.0
    total_time_ms: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["unique_textures"] = sorted(self.unique_textures)
        d["unique_flats"] = sorted(self.unique_flats)
        return d



# ---------------------------------------------------------------------------
# Geometry Validator
# ---------------------------------------------------------------------------

class MapValidator:
    """Validates Doom map geometry and reports issues."""

    def __init__(
        self,
        vertices: list[Vertex],
        linedefs: list[LineDef],
        sidedefs: list[SideDef],
        sectors: list[Sector],
        segs: list[Seg],
        subsectors: list[Subsector],
        nodes: list[Node],
        things: list[Thing],
    ) -> None:
        self.vertices = vertices
        self.linedefs = linedefs
        self.sidedefs = sidedefs
        self.sectors = sectors
        self.segs = segs
        self.subsectors = subsectors
        self.nodes = nodes
        self.things = things
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def validate_all(self) -> tuple[list[str], list[str]]:
        self._validate_vertices()
        self._validate_linedefs()
        self._validate_sidedefs()
        self._validate_sectors()
        self._validate_segs()
        self._validate_things()
        self._validate_bsp()
        return self.warnings, self.errors

    def _validate_vertices(self) -> None:
        if not self.vertices:
            self.errors.append("No vertices found")
            return
        seen: set[tuple[int, int]] = set()
        duplicates = 0
        for i, v in enumerate(self.vertices):
            if (v.x, v.y) in seen:
                duplicates += 1
            seen.add((v.x, v.y))
        if duplicates:
            self.warnings.append(f"{duplicates} duplicate vertex positions")

    def _validate_linedefs(self) -> None:
        if not self.linedefs:
            self.errors.append("No linedefs found")
            return
        missing_v1 = missing_v2 = 0
        for line in self.linedefs:
            if line.v1 >= len(self.vertices):
                missing_v1 += 1
            if line.v2 >= len(self.vertices):
                missing_v2 += 1
        if missing_v1 or missing_v2:
            self.errors.append(f"Linedefs reference {missing_v1 + missing_v2} missing vertices")
        for i, line in enumerate(self.linedefs):
            if line.v1 == line.v2:
                self.warnings.append(f"Linedef {i} has zero length (v1 == v2)")

    def _validate_sidedefs(self) -> None:
        if not self.sidedefs:
            self.errors.append("No sidedefs found")
            return
        for i, side in enumerate(self.sidedefs):
            if side.sector >= len(self.sectors) or side.sector < 0:
                self.errors.append(f"Sidedef {i} references missing sector {side.sector}")

    def _validate_sectors(self) -> None:
        if not self.sectors:
            self.errors.append("No sectors found")
            return
        for i, sec in enumerate(self.sectors):
            if sec.floor_height > sec.ceiling_height:
                self.errors.append(f"Sector {i} has floor > ceiling ({sec.floor_height} > {sec.ceiling_height})")
            if sec.light_level < 0 or sec.light_level > 255:
                self.warnings.append(f"Sector {i} has out-of-range light level {sec.light_level}")

    def _validate_segs(self) -> None:
        if not self.segs:
            self.warnings.append("No segs found (BSP not built?)")
            return
        for i, seg in enumerate(self.segs):
            if seg.linedef >= len(self.linedefs):
                self.errors.append(f"Seg {i} references missing linedef {seg.linedef}")
            if seg.v1 >= len(self.vertices) or seg.v2 >= len(self.vertices):
                self.errors.append(f"Seg {i} references missing vertex")

    def _validate_things(self) -> None:
        player_starts = [t for t in self.things if t.type == THING_TYPE_PLAYER1]
        if not player_starts:
            self.errors.append("No Player 1 start (thing type 1) found")
        elif len(player_starts) > 1:
            self.warnings.append(f"Multiple Player 1 starts found ({len(player_starts)})")

    def _validate_bsp(self) -> None:
        if not self.subsectors:
            self.warnings.append("No subsectors found")
        if not self.nodes:
            self.warnings.append("No nodes found")
        if self.segs and self.subsectors:
            total_segs_expected = sum(ss.numsegs for ss in self.subsectors)
            if total_segs_expected != len(self.segs):
                self.warnings.append(
                    f"Subsector seg count mismatch: expected {total_segs_expected}, got {len(self.segs)}"
                )



# ---------------------------------------------------------------------------
# Map Import Statistics Collector
# ---------------------------------------------------------------------------

def collect_stats(
    map_name: str,
    source_file: str,
    vertices: list[Vertex],
    linedefs: list[LineDef],
    sidedefs: list[SideDef],
    sectors: list[Sector],
    segs: list[Seg],
    subsectors: list[Subsector],
    nodes: list[Node],
    things: list[Thing],
    reject: bytes,
    blockmap: list[int],
) -> ImportStats:
    stats = ImportStats(map_name=map_name, source_file=source_file)
    stats.vertex_count = len(vertices)
    stats.linedef_count = len(linedefs)
    stats.sidedef_count = len(sidedefs)
    stats.sector_count = len(sectors)
    stats.seg_count = len(segs)
    stats.subsector_count = len(subsectors)
    stats.node_count = len(nodes)
    stats.thing_count = len(things)
    stats.reject_bytes = len(reject)
    stats.blockmap_words = len(blockmap)

    # Count linedef types
    for line in linedefs:
        if line.side_back == 0xFFFF or (line.flags & LINEDEF_FLAG_TWO_SIDED) == 0:
            stats.solid_linedef_count += 1
        else:
            stats.two_sided_linedef_count += 1
        if line.special in DOOR_SPECIALS:
            stats.door_count += 1
        if line.special in EXIT_SPECIALS:
            stats.exit_count += 1
        if line.special in LIFT_SPECIALS:
            stats.lift_count += 1

    # Count thing types
    for thing in things:
        if thing.type == THING_TYPE_PLAYER1:
            stats.player_start_count += 1
        elif thing.type in MONSTER_TYPES:
            stats.monster_count += 1
        elif thing.type in PICKUP_TYPES:
            stats.pickup_count += 1
        elif thing.type in BARREL_TYPES:
            stats.barrel_count += 1

    # Collect textures
    for side in sidedefs:
        for tex in (side.top_texture, side.bottom_texture, side.mid_texture):
            if tex and tex != "-" and tex != "F_SKY1":
                stats.unique_textures.add(tex)
    for sec in sectors:
        if sec.floor_pic and sec.floor_pic != "F_SKY1":
            stats.unique_flats.add(sec.floor_pic)
        if sec.ceiling_pic and sec.ceiling_pic != "F_SKY1":
            stats.unique_flats.add(sec.ceiling_pic)

    # Geometry bounds
    if vertices:
        xs = [v.x for v in vertices]
        ys = [v.y for v in vertices]
        stats.min_x = min(xs)
        stats.max_x = max(xs)
        stats.min_y = min(ys)
        stats.max_y = max(ys)
        stats.width_doom_units = stats.max_x - stats.min_x
        stats.height_doom_units = stats.max_y - stats.min_y

    # Light levels
    if sectors:
        lights = [s.light_level for s in sectors]
        stats.min_light = min(lights)
        stats.max_light = max(lights)

    return stats



# ---------------------------------------------------------------------------
# Cache Management
# ---------------------------------------------------------------------------

def compute_wad_hash(path: str, zip_member: str | None) -> str:
    """Compute a hash of the WAD file for cache invalidation."""
    h = hashlib.sha256()
    if path.lower().endswith(".zip"):
        with ZipFile(path) as zf:
            member = zip_member or DEFAULT_WAD_IN_ZIP
            if member not in zf.namelist():
                wads = [n for n in zf.namelist() if n.lower().endswith(".wad")]
                if not wads:
                    raise ValueError(f"no .wad found in {path}")
                member = wads[0]
            h.update(zf.read(member))
    else:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()[:16]


def cache_path(cache_dir: Path, map_name: str, wad_hash: str) -> Path:
    return cache_dir / f"{map_name.lower()}_{wad_hash}.json"


def load_cache(cache_file: Path) -> ImportStats | None:
    if not cache_file.exists():
        return None
    try:
        with open(cache_file) as f:
            data = json.load(f)
        stats = ImportStats()
        for k, v in data.items():
            if k == "unique_textures" or k == "unique_flats":
                setattr(stats, k, set(v))
            elif hasattr(stats, k):
                setattr(stats, k, v)
        return stats
    except (json.JSONDecodeError, KeyError):
        return None


def save_cache(cache_file: Path, stats: ImportStats) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(stats.to_dict(), f, indent=2)



# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_stats(stats: ImportStats, verbose: bool = False) -> None:
    print(f"\n{'='*60}")
    print(f"  DoomGeo Map Import Report: {stats.map_name}")
    print(f"{'='*60}")
    print(f"\n  Source: {stats.source_file}")
    print(f"\n  Geometry:")
    print(f"    Vertices:      {stats.vertex_count:>8,}")
    print(f"    Linedefs:      {stats.linedef_count:>8,}")
    print(f"    Sidedefs:      {stats.sidedef_count:>8,}")
    print(f"    Sectors:       {stats.sector_count:>8,}")
    print(f"    Segs:          {stats.seg_count:>8,}")
    print(f"    Subsectors:    {stats.subsector_count:>8,}")
    print(f"    Nodes:         {stats.node_count:>8,}")
    print(f"    Reject bytes:  {stats.reject_bytes:>8,}")
    print(f"    Blockmap words:{stats.blockmap_words:>8,}")
    print(f"\n  Map Bounds:")
    print(f"    X: {stats.min_x} to {stats.max_x} ({stats.width_doom_units} doom units)")
    print(f"    Y: {stats.min_y} to {stats.max_y} ({stats.height_doom_units} doom units)")
    print(f"\n  Line Types:")
    print(f"    Solid:         {stats.solid_linedef_count:>8,}")
    print(f"    Two-sided:     {stats.two_sided_linedef_count:>8,}")
    print(f"    Doors:         {stats.door_count:>8,}")
    print(f"    Exits:         {stats.exit_count:>8,}")
    print(f"    Lifts:         {stats.lift_count:>8,}")
    print(f"\n  Things:")
    print(f"    Player starts: {stats.player_start_count:>8,}")
    print(f"    Monsters:      {stats.monster_count:>8,}")
    print(f"    Pickups:       {stats.pickup_count:>8,}")
    print(f"    Barrels:       {stats.barrel_count:>8,}")
    print(f"    Total:         {stats.thing_count:>8,}")
    print(f"\n  Assets:")
    print(f"    Unique textures: {len(stats.unique_textures):>6,}")
    print(f"    Unique flats:    {len(stats.unique_flats):>6,}")
    print(f"\n  Lighting:")
    print(f"    Range: {stats.min_light} - {stats.max_light}")
    print(f"\n  Timing:")
    print(f"    Parse:    {stats.parse_time_ms:>8.1f} ms")
    print(f"    Validate: {stats.validate_time_ms:>8.1f} ms")
    print(f"    Convert:  {stats.convert_time_ms:>8.1f} ms")
    print(f"    Total:    {stats.total_time_ms:>8.1f} ms")

    if stats.warnings:
        print(f"\n  Warnings ({len(stats.warnings)}):")
        for w in stats.warnings:
            print(f"    ! {w}")

    if stats.errors:
        print(f"\n  Errors ({len(stats.errors)}):")
        for e in stats.errors:
            print(f"    X {e}")

    if verbose:
        print(f"\n  Textures:")
        for tex in sorted(stats.unique_textures):
            print(f"    {tex}")
        print(f"\n  Flats:")
        for flat in sorted(stats.unique_flats):
            print(f"    {flat}")

    print(f"\n{'='*60}\n")



# ---------------------------------------------------------------------------
# Main Import Pipeline
# ---------------------------------------------------------------------------

def load_wad(path: str, zip_member: str | None = None) -> bytes:
    if path.lower().endswith(".zip"):
        with ZipFile(path) as zf:
            member = zip_member or DEFAULT_WAD_IN_ZIP
            if member not in zf.namelist():
                wads = [n for n in zf.namelist() if n.lower().endswith(".wad")]
                if not wads:
                    raise ValueError(f"no .wad found in {path}")
                member = wads[0]
            return zf.read(member)
    with open(path, "rb") as f:
        return f.read()


def import_map(
    iwad_path: str,
    map_name: str,
    zip_member: str | None = None,
    validate: bool = True,
    cache_dir: str | None = None,
    verbose: bool = False,
) -> tuple[ImportStats, WadLoader]:
    """Import a Doom map and return statistics."""

    total_start = time.perf_counter()

    # Check cache
    wad_hash = compute_wad_hash(iwad_path, zip_member)
    cache_file = None
    if cache_dir:
        cache_file = cache_path(Path(cache_dir), map_name, wad_hash)
        cached = load_cache(cache_file)
        if cached:
            print(f"[doom_import] Using cached stats for {map_name} (hash={wad_hash})")
            return cached, None

    # Load WAD
    print(f"[doom_import] Loading WAD: {iwad_path}")
    data = load_wad(iwad_path, zip_member)
    wad = WadLoader(data)
    print(f"[doom_import] WAD loaded: {len(wad.lumps)} lumps, available maps: {wad.available_maps()}")

    # Parse map lumps
    parse_start = time.perf_counter()
    lumps = wad.map_lumps(map_name)

    vertices = parse_vertices(lumps["VERTEXES"])
    linedefs = parse_linedefs(lumps["LINEDEFS"])
    sidedefs = parse_sidedefs(lumps["SIDEDEFS"])
    sectors = parse_sectors(lumps["SECTORS"])
    things = parse_things(lumps["THINGS"])
    segs = parse_segs(lumps["SEGS"])
    subsectors = parse_subsectors(lumps["SSECTORS"])
    nodes = parse_nodes(lumps["NODES"])
    reject = lumps["REJECT"]
    blockmap = parse_blockmap_words(lumps["BLOCKMAP"])
    parse_time = (time.perf_counter() - parse_start) * 1000

    print(f"[doom_import] Parsed {map_name}: {len(vertices)} verts, {len(linedefs)} lines, "
          f"{len(sectors)} sectors, {len(things)} things")

    # Validate
    validate_start = time.perf_counter()
    warnings: list[str] = []
    errors: list[str] = []
    if validate:
        validator = MapValidator(vertices, linedefs, sidedefs, sectors, segs, subsectors, nodes, things)
        warnings, errors = validator.validate_all()
        validate_time = (time.perf_counter() - validate_start) * 1000
        if warnings:
            print(f"[doom_import] Validation warnings ({len(warnings)}):")
            for w in warnings:
                print(f"  ! {w}")
        if errors:
            print(f"[doom_import] Validation errors ({len(errors)}):")
            for e in errors:
                print(f"  X {e}")
    else:
        validate_time = 0.0

    # Collect statistics
    stats = collect_stats(
        map_name, iwad_path,
        vertices, linedefs, sidedefs, sectors,
        segs, subsectors, nodes, things,
        reject, blockmap,
    )
    stats.parse_time_ms = parse_time
    stats.validate_time_ms = validate_time
    stats.warnings = warnings
    stats.errors = errors

    total_time = (time.perf_counter() - total_start) * 1000
    stats.total_time_ms = total_time

    # Cache results
    if cache_file:
        save_cache(cache_file, stats)
        print(f"[doom_import] Cached stats to {cache_file}")

    return stats, wad



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="DoomGeo generic WAD import pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--iwad", required=True, help="Path to WAD or zip archive")
    parser.add_argument("--zip-member", help="WAD member inside zip")
    parser.add_argument("--map", default="E1M1", help="Map marker (default: E1M1)")
    parser.add_argument("--validate", action="store_true", help="Run geometry validation")
    parser.add_argument("--stats", action="store_true", help="Print detailed statistics")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--cache-dir", default=".tools/cache", help="Cache directory")
    parser.add_argument("--all-maps", action="store_true", help="Import all maps in the WAD")
    parser.add_argument("--json", action="store_true", help="Output stats as JSON")
    args = parser.parse_args()

    try:
        if args.all_maps:
            data = load_wad(args.iwad, args.zip_member)
            wad = WadLoader(data)
            maps = wad.available_maps()
            print(f"[doom_import] Found {len(maps)} maps: {maps}")
            all_stats: list[ImportStats] = []
            for map_name in maps:
                stats, _ = import_map(
                    args.iwad, map_name, args.zip_member,
                    validate=args.validate, cache_dir=args.cache_dir,
                    verbose=args.verbose,
                )
                all_stats.append(stats)
                print_stats(stats, verbose=args.verbose)
            if args.json:
                json_path = Path(args.cache_dir) / "all_maps_stats.json"
                json_path.parent.mkdir(parents=True, exist_ok=True)
                with open(json_path, "w") as f:
                    json.dump([s.to_dict() for s in all_stats], f, indent=2)
                print(f"[doom_import] Saved all stats to {json_path}")
        else:
            stats, _ = import_map(
                args.iwad, args.map, args.zip_member,
                validate=args.validate, cache_dir=args.cache_dir,
                verbose=args.verbose,
            )
            if args.json:
                print(json.dumps(stats.to_dict(), indent=2))
            else:
                print_stats(stats, verbose=args.verbose)

    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
