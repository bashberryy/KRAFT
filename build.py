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
import subprocess
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
BOARD_ALIASES = {
    "craasneto": "nissa",
    "craask":    "nissa",
}

# Safety limits
HTTP_TIMEOUT = 60
DOWNLOAD_SIZE_LIMIT = 8 * 1024 * 1024 * 1024  # 8 GiB
MIN_FREE_SPACE_BYTES = 15 * 1024 * 1024 * 1024  # 15 GB

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

_BOARD_RE = re.compile(r"^[a-z0-9_\-]+$")


def read_boards_file() -> dict:
    if not os.path.exists(BOARDS_JSON):
        raise FileNotFoundError(f"boards.json missing at {BOARDS_JSON}")
    with open(BOARDS_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)
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
    b = sanitize_board(board_in)
    download = BOARD_ALIASES.get(b, b)
    return b, download


def http_download(url: str, dest_path: str, timeout: int = HTTP_TIMEOUT, board: str = "") -> None:
    req = Request(url, headers={"User-Agent": "KRAFT-build/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            total = 0
            last_print = 0.0
            content_length = resp.headers.get("Content-Length")
            total_expected = int(content_length) if content_length else None
            with open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
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
        raise RuntimeError(f"Failed to download {url}: {e}")


def download_shim(board: str, dest_dir: str, allow_fallback: bool = False) -> str:
    cache_dir = os.path.expanduser("~/.cache/kraft/shims")
    os.makedirs(cache_dir, exist_ok=True)
    cached = os.path.join(cache_dir, f"{board}.zip")

    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        print(f"[build:{board}] using cached shim: {cached}")
        return cached

    mirrors = list(SHIM_MIRRORS)
    if allow_fallback or os.getenv("KRAFT_ALLOW_FALLBACK", "") == "1":
        mirrors += FALLBACK_SHIM_MIRRORS

    last_exc = None
    for template in mirrors:
        url = template.format(board=board)
        dest = os.path.join(dest_dir, f"{board}.download")
        print(f"[build:{board}] trying download: {url}")
        try:
            http_download(url, dest, board=board)
            if os.path.getsize(dest) == 0:
                raise RuntimeError("Downloaded file is empty")

            if zipfile.is_zipfile(dest):
                shutil.copy2(dest, cached)
                print(f"[build:{board}] download OK (zip); cached at {cached}")
                return cached

            if dest.lower().endswith(".bin") or not zipfile.is_zipfile(dest):
                wrapped = cached + ".tmp"
                with zipfile.ZipFile(wrapped, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                    zf.write(dest, f"{board}.bin")
                os.replace(wrapped, cached)
                print(f"[build:{board}] download OK (raw .bin); cached as zip at {cached}")
                return cached
        except Exception as e:
            last_exc = e
            print(f"[build:{board}] download failed for {url}: {e}")
            for candidate in (dest,):
                try:
                    os.remove(candidate)
                except FileNotFoundError:
                    pass
            time.sleep(1)
    raise RuntimeError(f"[build:{board}] All shim mirrors failed. Last error: {last_exc}")


def _safe_extract_to(zf: zipfile.ZipFile, member: str, dest_dir: str) -> str:
    bn = os.path.basename(member)
    if not bn:
        raise RuntimeError("Invalid zip member name (empty basename)")
    out = os.path.join(dest_dir, bn)
    with zf.open(member) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def choose_bin_from_zip(zf: zipfile.ZipFile, board: str) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".bin")]
    if not names:
        raise RuntimeError("No .bin entries found in shim zip")
    lc = [n.lower() for n in names]
    for i, n in enumerate(lc):
        if os.path.basename(n) == f"{board}.bin":
            return names[i]
    for i, n in enumerate(lc):
        if "chromeos" in n or "factory" in n or "recovery" in n:
            return names[i]
    if len(names) == 1:
        return names[0]
    raise RuntimeError(f"Multiple .bin files found in shim zip ({len(names)}); cannot automatically choose.")


def extract_bin(path: str, dest_dir: str, board: str) -> str:
    if path.endswith(".bin") and zipfile.is_zipfile(path) is False:
        return path

    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"{os.path.basename(path)} is not a valid zip file")

    with zipfile.ZipFile(path, "r") as zf:
        try:
            member = choose_bin_from_zip(zf, board)
        except Exception as e:
            nested = [n for n in zf.namelist() if n.lower().endswith(".zip")]
            if len(nested) == 1:
                inner = _safe_extract_to(zf, nested[0], dest_dir)
                return extract_bin(inner, dest_dir, board)
            raise
        return _safe_extract_to(zf, member, dest_dir)


def read_uint64_le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def _run_checked(cmd: list[str], board: str, accepted_codes: tuple[int, ...] = (0,)) -> None:
    print(f"[build:{board}] $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"[build:{board}] required utility is missing: {cmd[0]}") from exc
    if proc.returncode not in accepted_codes:
        raise RuntimeError(f"[build:{board}] command failed with exit code {proc.returncode}: {cmd[0]}")


def _align_lba(lba: int, alignment: int = 2048) -> int:
    return ((lba + alignment - 1) // alignment) * alignment


def _read_gpt(path: str) -> tuple[bytearray, list[dict], int, int]:
    sector = 512
    fsize = os.path.getsize(path)
    if fsize < 34 * sector:
        raise RuntimeError("Image too small to contain GPT")

    with open(path, "rb") as f:
        f.seek(sector)
        header = bytearray(f.read(92))
        if header[:8] != b"EFI PART":
            raise RuntimeError("No GPT header found")
        entry_lba = struct.unpack_from("<Q", header, 72)[0]
        count = struct.unpack_from("<I", header, 80)[0]
        entry_size = struct.unpack_from("<I", header, 84)[0]
        if count <= 0 or entry_size < 128 or entry_size > 4096:
            raise RuntimeError("Invalid GPT partition table dimensions")
        table_bytes = count * entry_size
        f.seek(entry_lba * sector)
        table = f.read(table_bytes)
        if len(table) != table_bytes:
            raise RuntimeError("GPT partition table is truncated")

    entries = []
    for i in range(count):
        raw = bytearray(table[i * entry_size:(i + 1) * entry_size])
        if len(raw) < 128:
            break
        first = struct.unpack_from("<Q", raw, 32)[0]
        last = struct.unpack_from("<Q", raw, 40)[0]
        if first == 0 and last == 0:
            continue
        if first > last or last * sector >= fsize:
            raise RuntimeError(f"Invalid GPT partition bounds at entry {i}: {first}-{last}")
        name = raw[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        entries.append({
            "index": i,
            "raw": raw,
            "start": first,
            "end": last,
            "name": name,
        })
    entries.sort(key=lambda e: (e["start"], e["index"]))
    return header, entries, entry_lba, entry_size


def _crc32c(data: bytes, crc: int = 0xFFFFFFFF) -> int:
    crc &= 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if (crc & 1) else 0)
    return crc ^ 0xFFFFFFFF


def _restore_chromeos_ro_compat(path: str, original_bits: int, board: str) -> None:
    if not original_bits:
        return

    with open(path, "rb") as f:
        f.seek(1024)
        sb = bytearray(f.read(1024))
    if len(sb) != 1024 or struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
        raise RuntimeError(f"[build:{board}] resized filesystem has no valid primary superblock")

    block_size = 1024 << struct.unpack_from("<I", sb, 0x18)[0]
    ro = struct.unpack_from("<I", sb, 0x64)[0] | original_bits
    struct.pack_into("<I", sb, 0x64, ro)

    if ro & 0x00000400:
        struct.pack_into("<I", sb, 0x3FC, 0)
        csum = _crc32c(bytes(sb[:0x3FC]))
        struct.pack_into("<I", sb, 0x3FC, csum)

    def patch_superblock(offset: int) -> None:
        with open(path, "r+b") as f:
            f.seek(offset)
            data = bytearray(f.read(1024))
            if len(data) != 1024 or struct.unpack_from("<H", data, 0x38)[0] != 0xEF53:
                return
            current = struct.unpack_from("<I", data, 0x64)[0]
            struct.pack_into("<I", data, 0x64, current | original_bits)
            if current & 0x00000400:
                struct.pack_into("<I", data, 0x3FC, 0)
                struct.pack_into("<I", data, 0x3FC, _crc32c(bytes(data[:0x3FC])))
            f.seek(offset)
            f.write(data)

    patch_superblock(1024)

    try:
        proc = subprocess.run(
            ["dumpe2fs", "-h", path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        for line in proc.stdout.splitlines():
            if "Superblock backups stored on blocks:" not in line:
                continue
            values = line.split(":", 1)[1].strip()
            for token in values.split():
                try:
                    block = int(token.rstrip(","))
                except ValueError:
                    continue
                if block > 0:
                    patch_superblock(block * block_size)
            break
    except FileNotFoundError:
        pass


def _filesystem_min_size(rootfs_path: str, board: str) -> int:
    if shutil.which("e2fsck") is None or shutil.which("resize2fs") is None:
        raise RuntimeError("e2fsprogs is required (e2fsck and resize2fs)")

    with open(rootfs_path, "rb") as f:
        f.seek(1024 + 0x64)
        original_ro_compat = struct.unpack("<I", f.read(4))[0]
    chromeos_bits = original_ro_compat & 0xFF000000

    if chromeos_bits:
        with open(rootfs_path, "r+b") as f:
            f.seek(1024 + 0x64)
            f.write(struct.pack("<I", original_ro_compat & 0x00FFFFFF))
        print(f"[build:{board}] temporarily cleared ChromeOS ext4 feature marker 0x{chromeos_bits:08x} for resize")

    try:
        _run_checked(["e2fsck", "-fy", rootfs_path], board, accepted_codes=(0, 1))
        _run_checked(["resize2fs", "-M", rootfs_path], board)
    finally:
        if chromeos_bits:
            _restore_chromeos_ro_compat(rootfs_path, chromeos_bits, board)

    size = os.path.getsize(rootfs_path)
    return (size + 511) // 512


def compact_gpt_image(path: str, board: str) -> str:
    """
    Repack the disk image with corrected GPT header/backup LBA handling.

    Fix vs original: the primary header's backup-LBA field (offset 32) and the
    backup header's self-LBA / primary-LBA / table-LBA fields are set once,
    in the right order, before the final CRC is calculated. The original code
    set primary offset-32 twice — once correctly, then overwrote it — causing
    CRU verification failures and boot refusals.
    """
    import zlib
    sector = 512
    header, entries, entry_lba, entry_size = _read_gpt(path)
    if not entries:
        raise RuntimeError(f"[build:{board}] GPT contains no partitions")

    root_entries = [e for e in entries if e["name"].upper() in {"ROOT-A", "ROOT-B"}]
    if not root_entries:
        raise RuntimeError(f"[build:{board}] no ROOT-A/ROOT-B partitions found")

    print(f"[build:{board}] GPT partitions:")
    for e in entries:
        size_mib = (e["end"] - e["start"] + 1) * sector / 1024 / 1024
        print(f"[build:{board}]   {e['name'] or '(unnamed)'}: {size_mib:.1f} MiB")

    first_root_index = min(e["index"] for e in root_entries)
    work_dir = tempfile.mkdtemp(prefix=f"kraft-compact-{board}-")
    compact_path = path + f".compact.{os.getpid()}.bin"

    try:
        for e in entries:
            part_path = os.path.join(work_dir, f"part-{e['index']:03d}.img")
            with open(path, "rb") as src:
                src.seek(e["start"] * sector)
                with open(part_path, "wb") as dst:
                    remaining = (e["end"] - e["start"] + 1) * sector
                    while remaining:
                        chunk = src.read(min(16 * 1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError(f"[build:{board}] truncated partition {e['index']}")
                        dst.write(chunk)
                        remaining -= len(chunk)

            old_sectors = e["end"] - e["start"] + 1
            with open(part_path, "rb") as part:
                part.seek(1024 + 0x38)
                magic = part.read(2)

            if magic == b"\x53\xef":
                e["src_path"] = part_path
                try:
                    new_sectors = _filesystem_min_size(part_path, board)
                    if new_sectors < old_sectors:
                        with open(part_path, "rb+") as f:
                            f.truncate(new_sectors * sector)
                        e["new_size_sectors"] = new_sectors
                        print(f"[build:{board}] {e['name']}: {old_sectors * sector / 1024 / 1024:.1f} MiB -> {new_sectors * sector / 1024 / 1024:.1f} MiB")
                    else:
                        print(f"[build:{board}] {e['name']}: ext filesystem did not shrink ({old_sectors * sector / 1024 / 1024:.1f} MiB)")
                except RuntimeError as exc:
                    if e["name"].upper() in {"ROOT-A", "ROOT-B"}:
                        raise RuntimeError(f"[build:{board}] could not compact {e['name']}: {exc}") from exc
                    print(f"[build:{board}] {e['name']}: could not shrink; preserving original size: {exc}")
            else:
                print(f"[build:{board}] {e['name']}: not an ext filesystem; preserving partition unchanged")
                os.remove(part_path)

        first_root = min(e["start"] for e in entries if e["index"] == first_root_index)
        cursor = first_root
        for e in entries:
            original_start = e["start"]
            original_size = e["end"] - e["start"] + 1
            if e["start"] < first_root:
                e["new_start"] = original_start
                e["new_size"] = original_size
                e["new_end"] = original_start + original_size - 1
            else:
                cursor = _align_lba(cursor)
                new_size = e.get("new_size_sectors", original_size)
                e["new_start"] = cursor
                e["new_size"] = new_size
                e["new_end"] = cursor + new_size - 1
                cursor = e["new_end"] + 1

        count = struct.unpack_from("<I", header, 80)[0]
        entry_sectors = (count * entry_size + sector - 1) // sector

        last_data_lba = max(e["new_end"] for e in entries)

        # backup GPT table goes right after the last data partition,
        # backup GPT header goes after that — this is the correct GPT layout.
        backup_table_lba = last_data_lba + 1
        backup_header_lba = backup_table_lba + entry_sectors
        final_disk_lba = backup_header_lba  # last LBA of the disk

        # Build updated partition table with new LBAs
        table = bytearray(count * entry_size)
        for e in entries:
            raw = bytearray(e["raw"])
            struct.pack_into("<Q", raw, 32, e["new_start"])
            struct.pack_into("<Q", raw, 40, e["new_end"])
            off = e["index"] * entry_size
            table[off:off + entry_size] = raw

        table_crc = zlib.crc32(table) & 0xFFFFFFFF

        # --- Primary GPT header ---
        primary = bytearray(92)
        primary[:len(header)] = header
        # my_lba = 1
        struct.pack_into("<Q", primary, 24, 1)
        # alternate_lba = backup header LBA  (THE key fix)
        struct.pack_into("<Q", primary, 32, backup_header_lba)
        # first_usable_lba = 34 (right after primary table)
        struct.pack_into("<Q", primary, 40, 34)
        # last_usable_lba = backup_table_lba - 1
        struct.pack_into("<Q", primary, 48, backup_table_lba - 1)
        # partition_entry_lba = 2 (unchanged)
        struct.pack_into("<Q", primary, 72, entry_lba)
        # num_partition_entries + size_of_partition_entry unchanged
        # partition_entry_array_crc32
        struct.pack_into("<I", primary, 88, table_crc)
        # header_crc32 — zero it first, then compute
        struct.pack_into("<I", primary, 16, 0)
        struct.pack_into("<I", primary, 16, zlib.crc32(primary[:92]) & 0xFFFFFFFF)

        # --- Backup GPT header ---
        backup = bytearray(92)
        backup[:len(header)] = header
        # my_lba = backup_header_lba
        struct.pack_into("<Q", backup, 24, backup_header_lba)
        # alternate_lba = 1 (points to primary)
        struct.pack_into("<Q", backup, 32, 1)
        # first_usable_lba same as primary
        struct.pack_into("<Q", backup, 40, 34)
        # last_usable_lba same as primary
        struct.pack_into("<Q", backup, 48, backup_table_lba - 1)
        # partition_entry_lba = backup_table_lba
        struct.pack_into("<Q", backup, 72, backup_table_lba)
        # partition_entry_array_crc32
        struct.pack_into("<I", backup, 88, table_crc)
        # header_crc32
        struct.pack_into("<I", backup, 16, 0)
        struct.pack_into("<I", backup, 16, zlib.crc32(backup[:92]) & 0xFFFFFFFF)

        # Write the compact image
        with open(compact_path, "wb") as out:
            # Copy protective MBR (LBA 0) + primary GPT header (LBA 1) area
            with open(path, "rb") as src:
                prefix_len = entry_lba * sector  # up to start of partition table
                out.write(src.read(prefix_len))

            # Write primary GPT header at LBA 1
            out.seek(sector)
            out.write(primary)

            # Write primary partition table at entry_lba (usually LBA 2)
            out.seek(entry_lba * sector)
            out.write(table)

            # Write partition payloads
            for e in entries:
                dst_off = e["new_start"] * sector
                out.seek(dst_off)
                if "src_path" in e:
                    with open(e["src_path"], "rb") as part:
                        shutil.copyfileobj(part, out, length=16 * 1024 * 1024)
                else:
                    with open(path, "rb") as src:
                        src.seek(e["start"] * sector)
                        remaining = (e["end"] - e["start"] + 1) * sector
                        while remaining:
                            chunk = src.read(min(16 * 1024 * 1024, remaining))
                            if not chunk:
                                raise RuntimeError(f"[build:{board}] truncated partition {e['index']}")
                            out.write(chunk)
                            remaining -= len(chunk)

            # Write backup partition table
            out.seek(backup_table_lba * sector)
            out.write(table)

            # Write backup GPT header
            out.seek(backup_header_lba * sector)
            out.write(backup)

            # Truncate to exact disk size
            out.truncate((final_disk_lba + 1) * sector)

        old_size = os.path.getsize(path)
        new_size = os.path.getsize(compact_path)
        print(f"[build:{board}] compact image: {old_size / 1024 / 1024:.1f} MiB -> {new_size / 1024 / 1024:.1f} MiB")
        return compact_path
    except Exception:
        try:
            os.remove(compact_path)
        except FileNotFoundError:
            pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def find_root_lba(path: str) -> Tuple[int, int]:
    fsize = os.path.getsize(path)
    if fsize < 2048:
        raise RuntimeError("Image too small to contain GPT")
    with open(path, "rb") as f:
        header = f.read(34 * 512)
    if header[512:520] != b"EFI PART":
        raise RuntimeError("No valid GPT signature ('EFI PART') found at LBA1")
    gpt = header[512:512+92]
    part_lba = read_uint64_le(gpt, 72)
    num = struct.unpack_from("<I", gpt, 80)[0]
    esz = struct.unpack_from("<I", gpt, 84)[0]
    if num == 0 or esz == 0:
        raise RuntimeError("Invalid GPT partition header values (num/esz)")
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
    root_a = [r for r in roots if r[3].upper() == "ROOT-A"]
    _, start, end, name = root_a[0] if root_a else sorted(roots, key=lambda x: x[0], reverse=True)[0]
    return int(start), int(end)


class Ext2FS:
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

    def write(self, ino: int, data: bytes, preserve_size: bool = False) -> None:
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
        if not preserve_size:
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
                fs.write(ino, real_stub, preserve_size=True)
                print(f"[build:{board}] stub -> {stub_path}")
            except ValueError:
                pass
    ino_i = fs.find("/etc/issue")
    if ino_i and fs.cap(ino_i) >= len(banner):
        fs.write(ino_i, banner)
        print(f"[build:{board}] banner -> /etc/issue")
    s_ino = fs.find("/etc/init/startup.conf")
    if s_ino and fs.cap(s_ino) >= len(startup):
        fs.write(s_ino, startup, preserve_size=True)
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


def _compress_with_xz(src_path: str, board: str) -> Optional[str]:
    """
    Compress src_path with xz (multi-threaded if available) and return the .xz path.
    Falls back gracefully if xz is unavailable.
    xz typically achieves 8-12x compression on ChromeOS shims (6 GiB -> ~500 MB).
    """
    xz_bin = shutil.which("xz")
    if not xz_bin:
        print(f"[build:{board}] xz not found; falling back to DEFLATE")
        return None

    out_path = src_path + ".xz"
    # -T0 = use all CPU cores, -6 = good compression without extreme RAM use
    cmd = [xz_bin, "-T0", "-6", "--keep", "-c", src_path]
    print(f"[build:{board}] compressing with xz (this takes a few minutes)...")
    try:
        with open(out_path, "wb") as f:
            proc = subprocess.run(cmd, stdout=f, check=False)
        if proc.returncode != 0:
            print(f"[build:{board}] xz failed with code {proc.returncode}; falling back to DEFLATE")
            try:
                os.remove(out_path)
            except FileNotFoundError:
                pass
            return None
        size_mib = os.path.getsize(out_path) / 1024 / 1024
        print(f"[build:{board}] xz compressed to {size_mib:.1f} MiB")
        return out_path
    except Exception as e:
        print(f"[build:{board}] xz error: {e}; falling back to DEFLATE")
        try:
            os.remove(out_path)
        except FileNotFoundError:
            pass
        return None


def build(board_in: str, local_zip: Optional[str] = None, allow_fallback: bool = False) -> str:
    boards_cfg = read_boards_file()
    display_board, board = resolve_board(board_in)
    if board not in boards_cfg:
        raise RuntimeError(f"[build:{display_board}] Unsupported board (not present in boards/boards.json)")

    if display_board != board:
        print(f"[build:{display_board}] resolved alias -> {board}")

    for p in [MENU_PATH, STARTUP_PATH, STUB_PATH, BANNER_PATH]:
        if not os.path.exists(p):
            raise FileNotFoundError(f"[build:{board}] Missing required file: {p}")

    ensure_free_space(SCRIPT_DIR)

    os.makedirs(DIST_DIR, exist_ok=True)
    out_zip = os.path.join(DIST_DIR, f"kraft_{board}.zip")
    temp_dir = tempfile.mkdtemp(prefix=f"kraft-{board}-")
    try:
        if local_zip:
            if not os.path.exists(local_zip):
                raise FileNotFoundError(f"[build:{board}] local shim {local_zip} not found")
            shim_bin = extract_bin(local_zip, temp_dir, board)
        else:
            shim_zip = download_shim(board, temp_dir, allow_fallback=allow_fallback)
            shim_bin = extract_bin(shim_zip, temp_dir, board)

        if not os.path.exists(shim_bin):
            raise RuntimeError(f"[build:{board}] extracted shim {shim_bin} missing")
        if os.path.getsize(shim_bin) < 1024:
            raise RuntimeError(f"[build:{board}] shim {shim_bin} too small")

        root_start, root_end = find_root_lba(shim_bin)
        p3_start = int(root_start) * 512
        p3_size = int(root_end - root_start) * 512
        fsize = os.path.getsize(shim_bin)
        if p3_start < 0 or p3_start + p3_size > fsize:
            raise RuntimeError(f"[build:{board}] ROOT partition bounds out of range")

        with open(shim_bin, "rb") as f:
            f.seek(p3_start)
            img = bytearray(f.read(p3_size))

        with open(MENU_PATH, "rb") as f: menu = f.read()
        with open(STARTUP_PATH, "rb") as f: startup = f.read()
        with open(BANNER_PATH, "rb") as f: banner = f.read()
        img = patch_rootfs(img, menu, startup, banner, board)

        with open(shim_bin, "r+b") as f:
            f.seek(p3_start)
            f.write(img)

        with open(shim_bin, "rb") as f:
            f.seek(p3_start + 0x438)
            magic_data = f.read(2)
            if len(magic_data) < 2:
                raise RuntimeError(f"[build:{board}] ext2 magic read out of range")
            magic = struct.unpack("<H", magic_data)[0]
        if magic != 0xEF53:
            raise RuntimeError(f"[build:{board}] ext2 magic check failed: 0x{magic:04x}")

        compacted_shim = compact_gpt_image(shim_bin, board)

        inner_name = f"chromeos_kraft_{board}.bin"
        tmp_out = out_zip + f".tmp{os.getpid()}"

        # Try xz compression first — yields ~500 MB vs ~5 GB for ChromeOS shims.
        # The .zip wraps the .bin.xz so CRU / flash tools receive a standard zip.
        # Users extract the .bin.xz and decompress with: xz -d chromeos_kraft_<board>.bin.xz
        xz_path = _compress_with_xz(compacted_shim, board)
        if xz_path:
            xz_inner_name = inner_name + ".xz"
            with zipfile.ZipFile(tmp_out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                zf.write(xz_path, xz_inner_name)
                # include a small README so users know to decompress before flashing
                readme = (
                    f"KRAFT shim for {board}\n"
                    "======================\n\n"
                    "The .bin inside this zip is xz-compressed to keep the download small.\n"
                    "Before flashing, decompress it:\n\n"
                    f"  Linux/macOS:  xz -d {xz_inner_name}\n"
                    f"  Windows:      Use 7-Zip to extract the inner .bin\n\n"
                    f"Then flash the resulting {inner_name} with Chromebook Recovery Utility\n"
                    "or dd/Rufus as normal.\n"
                )
                zf.writestr("README.txt", readme)
            os.remove(xz_path)
            # Verify the zip contains what we expect (xz variant)
            if not zipfile.is_zipfile(tmp_out):
                raise RuntimeError(f"[build:{board}] output zip is not a valid zip")
        else:
            # Fallback: sparse + DEFLATE level 6 (better than level 1, still fast enough)
            sparse_shim = compacted_shim + ".sparse"
            zero_block = b"\x00" * (4 * 1024 * 1024)
            with open(compacted_shim, "rb") as srcf, open(sparse_shim, "wb") as dstf:
                logical = 0
                while True:
                    chunk = srcf.read(len(zero_block))
                    if not chunk:
                        break
                    if chunk == zero_block[:len(chunk)]:
                        dstf.seek(len(chunk), os.SEEK_CUR)
                    else:
                        dstf.write(chunk)
                    logical += len(chunk)
                dstf.truncate(logical)
            physical_mib = os.stat(sparse_shim).st_blocks * 512 / 1024 / 1024
            logical_mib = os.path.getsize(sparse_shim) / 1024 / 1024
            print(f"[build:{board}] sparse image: logical {logical_mib:.1f} MiB, physical {physical_mib:.1f} MiB")
            os.remove(compacted_shim)
            compacted_shim = sparse_shim

            with zipfile.ZipFile(tmp_out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
                zf.write(compacted_shim, inner_name)
            verify_zip_contains_expected(tmp_out, inner_name, board)

        os.replace(tmp_out, out_zip)
        try:
            os.remove(compacted_shim)
        except FileNotFoundError:
            pass

        print(f"[build:{board}] done: {out_zip} ({os.path.getsize(out_zip)//1024//1024} MB)")
        return out_zip
    finally:
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
