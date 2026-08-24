#!/usr/bin/env python3
"""
KRAFT build script (hardened)

This script:
 - Downloads the factory shim for a single board (canonical dl.cros.download/{board}.zip by default).
 - Validates the archive / inner .bin selection carefully (no blind 'largest .bin' choice).
 - Patches ROOT-A with the KRAFT menu/startup/banner.
 - Produces an atomic dist/kraft_<board>.zip with inner name chromeos_kraft_<board>.bin.

Important runtime environment:
 - Intended to run in CI. Locally requires GITHUB_ACTIONS=true to run by default,
   or pass --local to skip that check.
 - To allow non-canonical fallback mirrors, set KRAFT_ALLOW_FALLBACK=1 in the environment
   (this is opt-in because other mirrors may be untrusted).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
import zipfile
from typing import Optional, Tuple
from urllib.error import URLError, HTTPError
from urllib.request import urlopen, Request

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MENU_PATH = os.path.join(SCRIPT_DIR, "src", "menu.sh")
STARTUP_PATH = os.path.join(SCRIPT_DIR, "src", "startup.conf")
STUB_PATH = os.path.join(SCRIPT_DIR, "src", "stub.sh")
BANNER_PATH = os.path.join(SCRIPT_DIR, "bootloader", "ui", "banner.txt")
BOARDS_JSON = os.path.join(SCRIPT_DIR, "boards", "boards.json")
DIST_DIR = os.path.join(SCRIPT_DIR, "dist")

# cros.download uses /files/<board>/<board>.zip, not just /<board>.zip
# confirmed from https://cros.download/shims
SHIM_MIRRORS = [
    "https://dl.cros.download/files/{board}/{board}.zip"
]
FALLBACK_SHIM_MIRRORS = [
    "https://cros.tech/shims/{board}.zip",
    "https://dl.blobfox.org/shims/{board}.zip",
    "https://mirror.akane.network/chromeos/{board}.zip",
]

# board aliases — map HWIDs / codenames to the actual family name used for shim downloads
# craasneto is the HWID codename for the nissa-craask variant
BOARD_ALIASES = {
    "craasneto": "nissa",
    "craask":    "nissa",
}

# Safety limits
HTTP_TIMEOUT = 60  # seconds — shims are big, 30 was too short
DOWNLOAD_SIZE_LIMIT = 8 * 1024 * 1024 * 1024  # 8 GiB (prevent infinite downloads) — nissa is ~6 GiB
MIN_FREE_SPACE_BYTES = 15 * 1024 * 1024 * 1024  # require 15GB free — shim + extracted + output

_SLOT_BLOCKLIST = {
    "/sbin/init",
    "/sbin/chromeos_startup",
    "/sbin/chromeos_startup.sh",
    "/etc/init/startup.conf",
    "/usr/sbin/factory_install.sh",
    "/usr/sbin/factory_reset.sh",
}

_SLOT_PREFER = [
    "/usr/sbin/chromeos-install",
    "/usr/bin/cros_payload",
    "/usr/sbin/write_gpt.sh",
    "/usr/sbin/dump_vpd_log",
    "/usr/sbin/display_boot_message",
    "/usr/sbin/secure-wipe.sh",
]

# allowed board name pattern
_BOARD_RE = re.compile(r"^[a-z0-9_\-]+$")


def read_boards_file() -> dict:
    if not os.path.exists(BOARDS_JSON):
        raise FileNotFoundError(f"boards.json missing at {BOARDS_JSON}")
    with open(BOARDS_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # detect simple invalid/duplicate entries: json.load already disallows dups at parse-time,
    # but validate entries contain required fields when present (name/family/arch)
    board_keys = [k for k in data.keys() if not k.startswith("_")]
    if not board_keys:
        raise RuntimeError("No boards found in boards/boards.json")
    for k in board_keys:
        v = data.get(k)
        if not isinstance(v, dict) or "name" not in v or "family" not in v or "arch" not in v:
            raise RuntimeError(f"Invalid entry for board '{k}' in boards.json")
    return data


def ensure_free_space(path: str, need: int = MIN_FREE_SPACE_BYTES) -> None:
    root = os.path.abspath(path)
    while not os.path.exists(root):
        root = os.path.dirname(root)
        if root == "/":
            break
    usage = shutil.disk_usage(root)
    if usage.free < need:
        raise RuntimeError(f"Not enough free disk space in {root}: need {need} bytes, have {usage.free} bytes")


def sanitize_board(board_in: str) -> str:
    if not isinstance(board_in, str):
        raise RuntimeError("Board must be a string")
    b = board_in.strip().lower()
    if not _BOARD_RE.match(b):
        raise RuntimeError(f"Invalid board name: '{board_in}' (sanitized '{b}')")
    return b


def resolve_board(board_in: str) -> tuple:
    """
    Resolve a board name or alias to (display_name, download_name).
    display_name is what the user passed; download_name is what we fetch the shim for.
    e.g. craasneto -> display=craasneto, download=nissa
    """
    b = sanitize_board(board_in)
    download = BOARD_ALIASES.get(b, b)
    return b, download


def http_download(url: str, dest_path: str, timeout: int = HTTP_TIMEOUT, board: str = "") -> None:
    """
    Stream-download from `url` to `dest_path` with timeout and size limit.
    """
    req = Request(url, headers={"User-Agent": "KRAFT-build/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            total = 0
            last_print = 0.0
            content_length = resp.headers.get("Content-Length")
            total_expected = int(content_length) if content_length else None
            with open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
                    if total > DOWNLOAD_SIZE_LIMIT:
                        raise RuntimeError(f"Download exceeded size limit ({DOWNLOAD_SIZE_LIMIT} bytes)")
                    now = time.monotonic()
                    if now - last_print >= 5:
                        mb = total / 1024 / 1024
                        if total_expected:
                            pct = total / total_expected * 100
                            print(f"[build:{board}]   {mb:.0f} MB / {total_expected/1024/1024:.0f} MB ({pct:.1f}%)")
                        else:
                            print(f"[build:{board}]   {mb:.0f} MB downloaded...")
                        last_print = now
    except HTTPError as e:
        raise RuntimeError(f"HTTP error {e.code} when downloading {url}: {e.reason}")
    except URLError as e:
        raise RuntimeError(f"URL error when downloading {url}: {e.reason}")
    except Exception as e:
        # bubble up as RuntimeError
        raise RuntimeError(f"Failed to download {url}: {e}")


def download_shim(board: str, dest_dir: str, allow_fallback: bool = False) -> str:
    mirrors = list(SHIM_MIRRORS)
    # allow explicit opt-in for the fallback mirrors via env var or --local flag
    if allow_fallback or os.getenv("KRAFT_ALLOW_FALLBACK", "") == "1":
        mirrors += FALLBACK_SHIM_MIRRORS  # type: ignore

    last_exc = None
    for template in mirrors:
        url = template.format(board=board)
        dest = os.path.join(dest_dir, f"{board}.zip")
        print(f"[build:{board}] trying download: {url}")
        try:
            http_download(url, dest, board=board)
            # quick check: not empty and valid zip or a plain .bin (we expect zip)
            if os.path.getsize(dest) == 0:
                os.remove(dest)
                raise RuntimeError("Downloaded file is empty")
            if zipfile.is_zipfile(dest):
                print(f"[build:{board}] download OK (zip)")
                return dest
            else:
                # not a zip, maybe a raw bin — require explicit local path for raw .bin to avoid accidental misuse
                os.remove(dest)
                raise RuntimeError("Downloaded file is not a zip archive; raw .bin downloads are not allowed from network for safety")
        except Exception as e:
            last_exc = e
            print(f"[build:{board}] download failed for {url}: {e}")
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except Exception:
                    pass
            # small backoff before trying next mirror
            time.sleep(1)
    raise RuntimeError(f"[build:{board}] All shim mirrors failed. Last error: {last_exc}")


def _safe_extract_to(zf: zipfile.ZipFile, member: str, dest_dir: str) -> str:
    # use basename to prevent path traversal
    bn = os.path.basename(member)
    if not bn:
        raise RuntimeError("Invalid zip member name (empty basename)")
    out = os.path.join(dest_dir, bn)
    with zf.open(member) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    # ensure proper mode if zip stores it
    return out


def choose_bin_from_zip(zf: zipfile.ZipFile, board: str) -> str:
    """
    Select the best .bin candidate from zf using ordered heuristic:
     1) exact match <board>.bin (case-insensitive)
     2) name contains 'chromeos' or 'factory' or 'recovery'
     3) if exactly one .bin exists, pick it
     4) otherwise, fail and require explicit input
    Returns the member name (not extracted path) to be extracted by caller.
    """
    names = [n for n in zf.namelist() if n.lower().endswith(".bin")]
    if not names:
        raise RuntimeError("No .bin entries found in shim zip")

    # normalize names
    lc = [n.lower() for n in names]
    # 1) exact board match
    for i, n in enumerate(lc):
        if os.path.basename(n) == f"{board}.bin":
            return names[i]
    # 2) prefer names containing keywords
    for i, n in enumerate(lc):
        if "chromeos" in n or "factory" in n or "recovery" in n:
            return names[i]
    # 3) if there's only one, pick it
    if len(names) == 1:
        return names[0]
    # 4) ambiguous — fail rather than guessing
    raise RuntimeError(f"Multiple .bin files found in shim zip ({len(names)}); cannot automatically choose. Provide explicit local shim.")


def extract_bin(path: str, dest_dir: str, board: str) -> str:
    """
    Given path to a zip (or a nested zip), extract the appropriate .bin and return its path.
    Accept only zip files from network; for raw .bin, require explicit local path (local_zip param).
    """
    if path.endswith(".bin") and zipfile.is_zipfile(path) is False:
        # local raw bin provided by caller — allowed
        return path

    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"{os.path.basename(path)} is not a valid zip file")

    with zipfile.ZipFile(path, "r") as zf:
        try:
            member = choose_bin_from_zip(zf, board)
        except Exception as e:
            # if nested zips exist, try to search nested zip heuristics
            # attempt to look for nested zips and recurse only if single nested zip
            nested = [n for n in zf.namelist() if n.lower().endswith(".zip")]
            if len(nested) == 1:
                inner = _safe_extract_to(zf, nested[0], dest_dir)
                return extract_bin(inner, dest_dir, board)
            raise
        # extract the chosen member
        return _safe_extract_to(zf, member, dest_dir)


def read_uint64_le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]



def trim_trailing_zeroes(path: str) -> None:
    with open(path, "rb+") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        pos = end
        chunk = 1024 * 1024
        while pos > 0:
            pos = max(0, pos - chunk)
            f.seek(pos)
            data = f.read(min(chunk, end - pos))
            stripped = data.rstrip(b"\x00")
            if stripped:
                f.truncate(pos + len(stripped))
                return


def find_root_lba(path: str) -> Tuple[int, int]:
    """
    Safely parse GPT and find ROOT-A partition (preferred) or largest 'ROOT' partition by name.
    Validate signature and partition table bounds.
    """
    fsize = os.path.getsize(path)
    if fsize < 2048:
        raise RuntimeError("Image too small to contain GPT")
    with open(path, "rb") as f:
        header = f.read(34 * 512)
    # GPT header signature at LBA1 (offset 512) should start with 'EFI PART'
    if header[512:520] != b"EFI PART":
        raise RuntimeError("No valid GPT signature ('EFI PART') found at LBA1")
    gpt = header[512:512+92]
    part_lba = read_uint64_le(gpt, 72)
    num = struct.unpack_from("<I", gpt, 80)[0]
    esz = struct.unpack_from("<I", gpt, 84)[0]
    # Sanity checks
    if num == 0 or esz == 0:
        raise RuntimeError("Invalid GPT partition header values (num/esz)")
    # ensure table read fits file
    table_bytes = num * esz
    with open(path, "rb") as f:
        f.seek(part_lba * 512)
        entries = f.read(table_bytes)
    roots = []
    for i in range(min(num, 128)):
        off = i * esz
        if off + esz > len(entries):
            break
        e = entries[off:off+esz]
        start = struct.unpack_from("<Q", e, 32)[0]
        end = struct.unpack_from("<Q", e, 40)[0]
        name = e[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        if start and end and start < end and end * 512 <= fsize and "ROOT" in name:
            roots.append((end - start, start, end, name))
    if not roots:
        raise RuntimeError("No valid ROOT partition found in GPT")
    # prefer ROOT-A specifically; fall back to largest if not found
    root_a = [r for r in roots if r[3].upper() == "ROOT-A"]
    _, start, end, name = root_a[0] if root_a else sorted(roots, key=lambda x: x[0], reverse=True)[0]
    return int(start), int(end)


class Ext2FS:
    """
    Small ext2 helper with defensive checks (NO full rewrite).
    - Uses superblock-derived block size.
    - Supports direct + single indirect pointers.
    - Checks bounds before reads/writes.
    """
    def __init__(self, img: bytearray):
        self.img = img
        if len(img) < 2048:
            raise RuntimeError("Image too small for ext2 superblock")
        sb = img[1024:1024+256]
        s_log_block_size = struct.unpack_from("<I", sb, 24)[0]
        self.BS = 1024 << (s_log_block_size & 0xFFFFFFFF)
        self.ipg = struct.unpack_from("<I", sb, 40)[0]
        self.isz = struct.unpack_from("<H", sb, 88)[0]
        self.gdt = struct.unpack_from("<I", sb, 20)[0] + 1
        # bounds check
        if self.BS <= 0 or self.isz <= 0:
            raise RuntimeError("Invalid ext2 block or inode size from superblock")

    def _bo(self, n: int) -> int:
        return n * self.BS

    def _rb(self, n: int) -> bytes:
        o = self._bo(n)
        if o + self.BS > len(self.img):
            raise RuntimeError("Block read out of range")
        return bytes(self.img[o:o + self.BS])

    def _ioff(self, ino: int) -> int:
        g = (ino - 1) // self.ipg
        idx = (ino - 1) % self.ipg
        gd_off = self.gdt * self.BS + g * 32
        if gd_off + 32 > len(self.img):
            raise RuntimeError("Group descriptor out of range")
        gd = self.img[gd_off:gd_off+32]
        block = struct.unpack_from("<I", gd, 8)[0]
        o = block * self.BS + idx * self.isz
        if o + self.isz > len(self.img):
            raise RuntimeError("Inode table out of range")
        return o

    def _ri(self, ino: int) -> bytes:
        o = self._ioff(ino)
        return bytes(self.img[o:o+self.isz])

    def _blk(self, inode: bytes) -> list:
        blocks = []
        for i in range(12):
            b = struct.unpack_from("<I", inode, 40 + i*4)[0]
            if b:
                blocks.append(b)
        single = struct.unpack_from("<I", inode, 40 + 12*4)[0]
        if single:
            # read block with pointers
            data = self._rb(single)
            count = self.BS // 4
            for i in range(count):
                ptr = struct.unpack_from("<I", data, i*4)[0]
                if ptr:
                    blocks.append(ptr)
        return blocks

    def _dir(self, bn: int):
        blk = self._rb(bn)
        entries = []
        off = 0
        while off + 8 <= len(blk):
            ino = struct.unpack_from("<I", blk, off)[0]
            rec = struct.unpack_from("<H", blk, off+4)[0]
            if rec == 0:
                break
            nl = blk[off+6]
            if ino:
                name = blk[off+8:off+8+nl].decode("utf-8", "replace")
                entries.append((ino, name))
            off += rec
        return entries

    def find(self, path: str) -> Optional[int]:
        curr = 2
        for part in [p for p in path.split("/") if p]:
            found = False
            inode = self._ri(curr)
            for b in self._blk(inode):
                for ino, name in self._dir(b):
                    if name == part:
                        curr = ino
                        found = True
                        break
                if found:
                    break
            if not found:
                return None
        return curr

    def read(self, ino: int) -> bytes:
        inode = self._ri(ino)
        size = struct.unpack_from("<I", inode, 4)[0]
        data = b"".join(self._rb(b) for b in self._blk(inode))
        return data[:size]

    def cap(self, ino: int) -> int:
        return len(self._blk(self._ri(ino))) * self.BS

    def write(self, ino: int, data: bytes) -> None:
        inode = self._ri(ino)
        blocks = self._blk(inode)
        c = len(blocks) * self.BS
        if len(data) > c:
            raise ValueError("data too large for slot")
        rem = bytearray(data)
        for blk in blocks:
            o = self._bo(blk)
            chunk = bytes(rem[:self.BS]).ljust(self.BS, b"\x00")
            if o + self.BS > len(self.img):
                raise RuntimeError("Write out of range")
            self.img[o:o+self.BS] = chunk
            rem = rem[self.BS:]
            if not rem:
                break
        struct.pack_into("<I", self.img, self._ioff(ino) + 4, len(data))


def _scan_shell_slots(fs: Ext2FS):
    results = []
    visited = set()
    def _walk(ino, path):
        if ino in visited:
            return
        visited.add(ino)
        try:
            inode = fs._ri(ino)
            mode = struct.unpack_from("<H", inode, 0)[0]
            ftype = mode & 0xF000
            if ftype == 0x8000:
                if fs._blk(inode):
                    try:
                        if fs.read(ino)[:2] == b"#!":
                            results.append((fs.cap(ino), path, ino))
                    except Exception:
                        pass
            elif ftype == 0x4000:
                for blk in fs._blk(inode):
                    for cino, cname in fs._dir(blk):
                        if cname not in (".", ".."):
                            _walk(cino, path + "/" + cname)
        except Exception:
            pass
    _walk(2, "")
    results.sort(reverse=True)
    return results


def _pick_slot(fs: Ext2FS, menu_size: int):
    slots = _scan_shell_slots(fs)
    candidates = [(cap, path, ino) for cap, path, ino in slots if cap >= menu_size and path not in _SLOT_BLOCKLIST]
    if not candidates:
        raise RuntimeError("No shell slot large enough for menu")
    prefer_index = {p: i for i, p in enumerate(_SLOT_PREFER)}
    candidates.sort(key=lambda x: (prefer_index.get(x[1], len(_SLOT_PREFER)), -x[0]))
    return candidates[0][1], candidates[0][2]


def patch_rootfs(img: bytearray, menu: bytes, startup: bytes, banner: bytes, board: str) -> bytearray:
    fs = Ext2FS(img)
    slot_path, slot_ino = _pick_slot(fs, len(menu))
    print(f"[build:{board}] menu -> {slot_path} ({fs.cap(slot_ino)} b, {len(menu)} b needed)")
    fs.write(slot_ino, menu)
    real_stub = f"#!/bin/bash\nexec {slot_path}\n".encode()
    for stub_path in ["/usr/sbin/factory_install.sh", "/usr/sbin/factory_reset.sh"]:
        if stub_path == slot_path:
            continue
        ino = fs.find(stub_path)
        if ino:
            try:
                fs.write(ino, real_stub)
                print(f"[build:{board}] stub -> {stub_path}")
            except ValueError:
                pass
    ino_i = fs.find("/etc/issue")
    if ino_i and fs.cap(ino_i) >= len(banner):
        fs.write(ino_i, banner)
        print(f"[build:{board}] banner -> /etc/issue")
    s_ino = fs.find("/etc/init/startup.conf")
    if s_ino and fs.cap(s_ino) >= len(startup):
        fs.write(s_ino, startup)
        print(f"[build:{board}] startup.conf patched")
    return img


def verify_zip_contains_expected(out_zip: str, inner_name: str, board: str):
    if not zipfile.is_zipfile(out_zip):
        raise RuntimeError(f"[build:{board}] output zip {out_zip} is not a valid zip")
    with zipfile.ZipFile(out_zip, "r") as zf:
        entries = [n for n in zf.namelist() if n.lower().endswith(".bin")]
        if len(entries) != 1:
            raise RuntimeError(f"[build:{board}] unexpected .bin count in output zip: {len(entries)}")
        if entries[0] != inner_name:
            raise RuntimeError(f"[build:{board}] unexpected inner bin name: {entries[0]} (expected {inner_name})")


def build(board_in: str, local_zip: Optional[str] = None, allow_fallback: bool = False) -> str:
    # Basic sanity checks
    boards_cfg = read_boards_file()
    display_board, board = resolve_board(board_in)
    if board not in boards_cfg:
        raise RuntimeError(f"[build:{display_board}] Unsupported board (not present in boards/boards.json)")

    if display_board != board:
        print(f"[build:{display_board}] resolved alias -> {board}")

    # required files
    for p in [MENU_PATH, STARTUP_PATH, STUB_PATH, BANNER_PATH]:
        if not os.path.exists(p):
            raise FileNotFoundError(f"[build:{board}] Missing required file: {p}")

    ensure_free_space(SCRIPT_DIR)

    os.makedirs(DIST_DIR, exist_ok=True)
    out_zip = os.path.join(DIST_DIR, f"kraft_{board}.zip")
    temp_dir = tempfile.mkdtemp(prefix=f"kraft-{board}-")
    try:
        # fetch or extract shim
        if local_zip:
            # ensure local file exists and is a .bin or zip
            if not os.path.exists(local_zip):
                raise FileNotFoundError(f"[build:{board}] local shim {local_zip} not found")
            shim_bin = extract_bin(local_zip, temp_dir, board)
        else:
            shim_zip = download_shim(board, temp_dir, allow_fallback=allow_fallback)
            shim_bin = extract_bin(shim_zip, temp_dir, board)

        # verify shim_bin exists and is reasonable
        if not os.path.exists(shim_bin):
            raise RuntimeError(f"[build:{board}] extracted shim {shim_bin} missing")
        if os.path.getsize(shim_bin) < 1024:
            raise RuntimeError(f"[build:{board}] shim {shim_bin} too small")

        # parse GPT & find ROOT
        root_start, root_end = find_root_lba(shim_bin)
        p3_start = int(root_start) * 512
        p3_size = int(root_end - root_start) * 512
        fsize = os.path.getsize(shim_bin)
        if p3_start < 0 or p3_start + p3_size > fsize:
            raise RuntimeError(f"[build:{board}] ROOT partition bounds out of range: start={p3_start} size={p3_size} file={fsize}")

        # read ROOT-A into memory
        with open(shim_bin, "rb") as f:
            f.seek(p3_start)
            img = bytearray(f.read(p3_size))

        # patch rootfs
        with open(MENU_PATH, "rb") as f: menu = f.read()
        with open(STARTUP_PATH, "rb") as f: startup = f.read()
        with open(BANNER_PATH, "rb") as f: banner = f.read()
        img = patch_rootfs(img, menu, startup, banner, board)

        # write back into shim file (atomic via tmp file)
        tmp_shim = shim_bin + f".patched.{os.getpid()}.tmp"
        # copy original to tmp_shim and then patch the region
        shutil.copyfile(shim_bin, tmp_shim)
        try:
            with open(tmp_shim, "r+b") as f:
                f.seek(p3_start)
                f.write(img)
        except Exception:
            try:
                os.remove(tmp_shim)
            except Exception:
                pass
            raise
        # replace original shim with patched one (atomic on same filesystem)
        os.replace(tmp_shim, shim_bin)

        # quick ext2 magic check
        with open(shim_bin, "rb") as f:
            f.seek(p3_start + 0x438)
            magic_data = f.read(2)
            if len(magic_data) < 2:
                raise RuntimeError(f"[build:{board}] ext2 magic read out of range")
            magic = struct.unpack("<H", magic_data)[0]
        if magic != 0xEF53:
            raise RuntimeError(f"[build:{board}] ext2 magic check failed: 0x{magic:04x}")

        trim_trailing_zeroes(shim_bin)

        # create the zip atomically
        inner_name = f"chromeos_kraft_{board}.bin"
        tmp_out = out_zip + f".tmp{os.getpid()}"
        with zipfile.ZipFile(tmp_out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.write(shim_bin, inner_name)
        # verify zipped content
        verify_zip_contains_expected(tmp_out, inner_name, board)
        # atomic rename
        os.replace(tmp_out, out_zip)

        print(f"[build:{board}] done: {out_zip} ({os.path.getsize(out_zip)//1024//1024} MB)")
        return out_zip
    finally:
        # cleanup
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="KRAFT build script")
    parser.add_argument("board", help="Board name or alias (e.g. nissa, craasneto)")
    parser.add_argument("local_zip", nargs="?", default=None, help="Optional local shim .zip or .bin")
    parser.add_argument("--local", action="store_true",
                        help="Run outside of CI (WSL/local machine). Also enables fallback mirrors.")
    parser.add_argument("--fallback", action="store_true",
                        help="Enable fallback mirrors even without --local.")
    args = parser.parse_args()

    in_ci = os.getenv("GITHUB_ACTIONS") == "true"
    if not in_ci and not args.local:
        print(f"This build script is intended to run in CI. Pass --local to run here:\n  python3 build.py {args.board} --local", file=sys.stderr)
        sys.exit(2)

    try:
        build(args.board, args.local_zip, allow_fallback=(args.local or args.fallback))
    except Exception as e:
        print(f"[build:{args.board}] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

## I LOVE PYTHon? do I?
