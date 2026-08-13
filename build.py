#!/usr/bin/env python3
"""
KRAFT build script
Downloads a factory shim for the given board, patches it with KRAFT menu,
and produces a CRU-compatible zip.

Usage:
    python3 build.py <board>
    python3 build.py <board> <shim.bin>
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import urllib.request
import zipfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MENU_PATH = os.path.join(SCRIPT_DIR, "src", "menu.sh")
STARTUP_PATH = os.path.join(SCRIPT_DIR, "src", "startup.conf")
STUB_PATH = os.path.join(SCRIPT_DIR, "src", "stub.sh")
BANNER_PATH = os.path.join(SCRIPT_DIR, "bootloader", "ui", "banner.txt")
BOARDS_JSON = os.path.join(SCRIPT_DIR, "boards", "boards.json")

# Primary mirror format:
SHIM_MIRRORS = [
    "https://dl.cros.download/{board}.zip",
    # keep a few fallbacks for robustness obviously
    "https://cros.tech/shims/{board}.zip",
    "https://dl.blobfox.org/shims/{board}.zip",
    "https://mirror.akane.network/chromeos/{board}.zip",
]

# Fallback hard-coded known boards in case boards/boards.json is missing. (it contains cdn sooooo)
FALLBACK_KNOWN = [
    "ambassador", "banon", "brask", "brya", "clapper", "coral",
    "corsola", "cyan", "dedede", "edgar", "elm", "enguarde", "fizz",
    "glimmer", "grunt", "hana", "hatch", "jacuzzi", "kalista", "kefka",
    "kukui", "lulu", "nami", "nissa", "octopus", "orco", "puff", "pyro",
    "reef", "reks", "relm", "sand", "sentry", "snappy", "stout",
    "strongbad", "tidus", "trogdor", "ultima", "volteer", "zork",
]

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

def load_known_boards():
    if os.path.exists(BOARDS_JSON):
        try:
            with open(BOARDS_JSON, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            boards = [k for k in data.keys() if not k.startswith("_")]
            if boards:
                return sorted(boards)
        except Exception:
            # fallthrough to fallback
            pass
    return FALLBACK_KNOWN

KNOWN_BOARDS = load_known_boards()

def _progress(count, block, total):
    if total > 0:
        pct = min(100, count * block * 100 // total)
        bar = "#" * (pct // 2) + "-" * (50 - pct // 2)
        mb = count * block // 1024 // 1024
        print(f"\r  [{bar}] {pct}% {mb}MB", end="", flush=True)
    else:
        print(f"\r  {count * block // 1024 // 1024}MB...", end="", flush=True)

def download_shim(board, dest_dir):
    for template in SHIM_MIRRORS:
        url = template.format(board=board)
        dest = os.path.join(dest_dir, f"{board}.zip")
        print(f"[build] trying: {url}")
        try:
            urllib.request.urlretrieve(url, dest, _progress)
            print()
            print("[build] download OK")
            return dest
        except Exception as e:
            print(f"\n[build] failed: {e}")
            if os.path.exists(dest):
                os.remove(dest)
    raise RuntimeError(f"All shim mirrors failed for board '{board}'")

def _safe_extract_to(zf, member, dest_path):
    # Prevent path traversal by using basename
    bn = os.path.basename(member)
    out = os.path.join(dest_path, bn)
    with zf.open(member) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out

def extract_bin(path, dest_dir):
    """
    Return path to a .bin file inside 'path' (which may be a .bin file or a zip)
    Handles nested zips; chooses the largest .bin file if multiple exist.
    """
    if path.endswith(".bin") and zipfile.is_zipfile(path) is False:
        print(f"[build] using bin directly: {os.path.basename(path)}")
        return path

    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"{os.path.basename(path)} is not a valid zip")

    print(f"[build] extracting bin from {os.path.basename(path)}")
    with zipfile.ZipFile(path, "r") as zf:
        infos = zf.infolist()
        # find .bin entries and .zip entries
        bin_infos = [i for i in infos if i.filename.lower().endswith(".bin")]
        zip_infos = [i for i in infos if i.filename.lower().endswith(".zip")]

        if bin_infos:
            # pick the largest .bin (most likely the real firmware)
            best = max(bin_infos, key=lambda i: i.file_size or 0)
            return _safe_extract_to(zf, best.filename, dest_dir)

        if zip_infos:
            # extract the largest nested zip and recurse
            best = max(zip_infos, key=lambda i: i.file_size or 0)
            inner = _safe_extract_to(zf, best.filename, dest_dir)
            return extract_bin(inner, dest_dir)

    raise RuntimeError(f"No .bin found in {os.path.basename(path)}")

def find_root_lba(path):
    """
    Parse GPT to find the largest ROOT partition (matching name with 'ROOT').
    """
    with open(path, "rb") as f:
        header = f.read(34 * 512)  # should include primary header + partition table header
    if len(header) < 1024:
        raise RuntimeError("Image too small or corrupted (cannot read GPT)")

    gpt = header[512:512+92]
    if len(gpt) < 92:
        raise RuntimeError("Invalid GPT header")

    part_lba = struct.unpack_from("<Q", gpt, 72)[0]
    num = struct.unpack_from("<I", gpt, 80)[0]
    esz = struct.unpack_from("<I", gpt, 84)[0]

    with open(path, "rb") as f:
        f.seek(part_lba * 512)
        entries = f.read(num * esz)

    roots = []
    for i in range(min(num, 128)):  # be defensive
        off = i * esz
        if off + esz > len(entries):
            break
        e = entries[off:off+esz]
        start = struct.unpack_from("<Q", e, 32)[0]
        end = struct.unpack_from("<Q", e, 40)[0]
        try:
            name = e[56:128].decode("utf-16-le", "replace").rstrip("\x00")
        except Exception:
            name = ""
        if start and "ROOT" in name:
            roots.append((end - start, start, end, name))

    if not roots:
        raise RuntimeError("No ROOT partition found in GPT")
    roots.sort(key=lambda x: x[0], reverse=True)  # largest first
    _, start, end, name = roots[0]
    size_mb = (end - start) * 512 // 1024 // 1024
    print(f"[build] ROOT '{name}': LBA {start}-{end} ({size_mb} MB)")
    return start, end

class Ext2FS:
    """
    Minimal ext2 reader/writer sufficient for inserting small scripts.
    - Computes block size from superblock.
    - Supports direct blocks and single indirect.
    """
    def __init__(self, img):
        self.img = img
        # read superblock at offset 1024
        sb = img[1024:1024+256]
        if len(sb) < 256:
            raise RuntimeError("image too small for ext superblock")
        s_log_block_size = struct.unpack_from("<I", sb, 24)[0]
        self.BS = 1024 << (s_log_block_size & 0xFFFFFFFF)
        self.ipg = struct.unpack_from("<I", sb, 40)[0]
        self.isz = struct.unpack_from("<H", sb, 88)[0]
        self.gdt = struct.unpack_from("<I", sb, 20)[0] + 1

    def _bo(self, n):
        return n * self.BS

    def _rb(self, n):
        o = self._bo(n)
        return bytes(self.img[o : o + self.BS])

    def _ioff(self, ino):
        g = (ino - 1) // self.ipg
        idx = (ino - 1) % self.ipg
        gd_off = self.gdt * self.BS + g * 32
        gd = self.img[gd_off : gd_off + 32]
        block = struct.unpack_from("<I", gd, 8)[0]
        return block * self.BS + idx * self.isz

    def _ri(self, ino):
        o = self._ioff(ino)
        return bytes(self.img[o : o + self.isz])

    def _blk(self, inode):
        # return list of block numbers from inode (direct + single-indirect)
        blocks = []
        for i in range(12):
            try:
                b = struct.unpack_from("<I", inode, 40 + i*4)[0]
            except struct.error:
                b = 0
            if b:
                blocks.append(b)
        # single indirect
        try:
            single = struct.unpack_from("<I", inode, 40 + 12*4)[0]
        except struct.error:
            single = 0
        if single:
            # read block containing u32 block pointers
            buf = self._rb(single)
            count = self.BS // 4
            for i in range(count):
                ptr = struct.unpack_from("<I", buf, i*4)[0]
                if ptr:
                    blocks.append(ptr)
        return blocks

    def _dir(self, bn):
        blk = self._rb(bn)
        entries = []
        off = 0
        while off < self.BS:
            if off + 8 > len(blk):
                break
            ino = struct.unpack_from("<I", blk, off)[0]
            rec = struct.unpack_from("<H", blk, off + 4)[0]
            if rec == 0:
                break
            nl = blk[off + 6]
            if ino:
                name = blk[off + 8 : off + 8 + nl].decode("utf-8", "replace")
                entries.append((ino, name))
            off += rec
        return entries

    def find(self, path):
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

    def read(self, ino):
        inode = self._ri(ino)
        size = struct.unpack_from("<I", inode, 4)[0]
        data = b"".join(self._rb(b) for b in self._blk(inode))
        return data[:size]

    def cap(self, ino):
        blocks = self._blk(self._ri(ino))
        return len(blocks) * self.BS

    def write(self, ino, data):
        inode = self._ri(ino)
        blocks = self._blk(inode)
        c = len(blocks) * self.BS
        if len(data) > c:
            raise ValueError(f"data {len(data)}b exceeds capacity {c}b")
        rem = bytearray(data)
        for blk in blocks:
            chunk = bytes(rem[: self.BS]).ljust(self.BS, b"\x00")
            o = self._bo(blk)
            self.img[o : o + self.BS] = chunk
            rem = rem[self.BS :]
            if not rem:
                break
        struct.pack_into("<I", self.img, self._ioff(ino) + 4, len(data))

def _scan_shell_slots(fs):
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

def _pick_slot(fs, menu_size):
    slots = _scan_shell_slots(fs)
    candidates = [(cap, path, ino) for cap, path, ino in slots
                  if cap >= menu_size and path not in _SLOT_BLOCKLIST]
    if not candidates:
        raise RuntimeError(f"No shell slot large enough for menu ({menu_size} bytes)")
    prefer_index = {p: i for i, p in enumerate(_SLOT_PREFER)}
    candidates.sort(key=lambda x: (prefer_index.get(x[1], len(_SLOT_PREFER)), -x[0]))
    _, slot_path, slot_ino = candidates[0]
    return slot_path, slot_ino

def patch_rootfs(img, menu, startup, banner, board):
    fs = Ext2FS(img)
    print(f"[build] scanning rootfs for slot (need {len(menu)} bytes)...")
    slot_path, slot_ino = _pick_slot(fs, len(menu))
    print(f"[build] menu -> {slot_path} ({fs.cap(slot_ino)} b, {len(menu)} b needed)")
    fs.write(slot_ino, menu)
    real_stub = f"#!/bin/bash\nexec {slot_path}\n".encode()
    for stub_path in ["/usr/sbin/factory_install.sh", "/usr/sbin/factory_reset.sh"]:
        if stub_path == slot_path:
            continue
        ino = fs.find(stub_path)
        if ino:
            try:
                fs.write(ino, real_stub)
                print(f"[build] stub  -> {stub_path}")
            except ValueError:
                pass
    ino_i = fs.find("/etc/issue")
    if ino_i and fs.cap(ino_i) >= len(banner):
        fs.write(ino_i, banner)
        print("[build] banner -> /etc/issue")
    s_ino = fs.find("/etc/init/startup.conf")
    if s_ino and fs.cap(s_ino) >= len(startup):
        fs.write(s_ino, startup)
        print("[build] startup.conf patched")
    return img

def build(board, local_zip=None):
    for p in [MENU_PATH, STARTUP_PATH, STUB_PATH, BANNER_PATH]:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Missing: {p}")
    board = board.lower().strip("/")
    if board not in KNOWN_BOARDS:
        print(f"[build] WARN: '{board}' not in known boards list")
    out_zip = os.path.join(SCRIPT_DIR, "dist", f"kraft_{board}.zip")
    os.makedirs(os.path.join(SCRIPT_DIR, "dist"), exist_ok=True)
    print(f"[build] board:  {board}")
    print(f"[build] output: {out_zip}")
    with open(MENU_PATH, "rb") as f: menu = f.read()
    with open(STARTUP_PATH, "rb") as f: startup = f.read()
    with open(BANNER_PATH, "rb") as f: banner = f.read()
    with tempfile.TemporaryDirectory() as tmp:
        if local_zip:
            shim_bin = extract_bin(local_zip, tmp)
        else:
            shim_zip = download_shim(board, tmp)
            shim_bin = extract_bin(shim_zip, tmp)
        size_mb = os.path.getsize(shim_bin) // 1024 // 1024
        print(f"[build] shim size: {size_mb} MB")
        root_start, root_end = find_root_lba(shim_bin)
        p3_start = root_start * 512
        p3_size = (root_end - root_start) * 512
        print(f"[build] reading ROOT-A ({p3_size // 1024 // 1024} MB)...")
        with open(shim_bin, "rb") as f:
            f.seek(p3_start)
            img = bytearray(f.read(p3_size))
        img = patch_rootfs(img, menu, startup, banner, board)
        print(f"[build] writing patched ROOT-A back...")
        with open(shim_bin, "r+b") as f:
            f.seek(p3_start)
            f.write(img)
        del img
        with open(shim_bin, "rb") as f:
            f.seek(p3_start + 0x438)
            magic = struct.unpack("<H", f.read(2))[0]
        if magic != 0xEF53:
            raise RuntimeError(f"ext2 magic check failed: 0x{magic:04x}")
        print("[build] ext2 magic: OK")
        inner_name = f"chromeos_kraft_{board}.bin"
        print(f"[build] zipping {os.path.getsize(shim_bin)//1024//1024} MB -> {out_zip}...")
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_STORED) as zf:
            zf.write(shim_bin, inner_name)
    final_mb = os.path.getsize(out_zip) // 1024 // 1024
    print(f"[build] done: {out_zip} ({final_mb} MB)")
    return out_zip

if __name__ == "__main__":
    # Disallow running locally by default; allow explicitly in dev by setting GITHUB_ACTIONS=true
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("This build script is intended to run under CI only. Set GITHUB_ACTIONS=true to override.", file=sys.stderr)
        sys.exit(2)
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"Known boards: {', '.join(KNOWN_BOARDS)}")
        sys.exit(1)
    board_arg = sys.argv[1]
    local_zip_arg = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        build(board_arg, local_zip_arg)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print(f"\n[build] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
