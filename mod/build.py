"""Builds the FIND IEMS patch and applies it to the stock application images.

    python mod/build.py [ra|kinetis|p10r ...]

Reads the untouched images from the repository root and writes the patched
ones next to this script.  Needs arm-none-eabi-gcc and avr-gcc, on the PATH
or where winget installs them.
"""
import glob
import os
import re
import shutil
import struct
import subprocess
import sys

import cosmetics

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(HERE, "src")
OUT = os.path.join(HERE, "build")

ARM_TARGETS = {
    "ra": dict(image="App_P10RP_RA.bin", header="target_ra.h", base=0x18000),
    "kinetis": dict(image="App_P10RP.bin", header="target_kinetis.h", base=0x18000),
}
# vtable slot -> hook
VTABLE_HOOKS = [("VT_TICK", "hook_tick"), ("VT_EXIT", "hook_exit"),
                ("VT_ENTER", "hook_enter"), ("VT_UP", "hook_up"),
                ("VT_DOWN", "hook_down"), ("VT_TIMER_1S", "hook_timer_1s")]
# call site -> (function it must currently call, hook)
CALL_HOOKS = [("SITE_LIST_DRAW", "ORIG_LIST_DRAW", "hook_list_draw"),
              ("SITE_DRAW_FLUSH", "FW_LCD_FLUSH", "hook_draw_flush")]
VT_ORIG = {"VT_TICK": "ORIG_TICK", "VT_EXIT": "ORIG_EXIT", "VT_ENTER": "ORIG_ENTER",
           "VT_UP": "ORIG_UP", "VT_DOWN": "ORIG_DOWN", "VT_TIMER_1S": "ORIG_TIMER_1S"}


def tool(name):
    exe = shutil.which(name)
    if exe:
        return exe
    for pat in (r"C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi\*\bin",
                r"C:\Program Files\Arm GNU Toolchain arm-none-eabi\*\bin",
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
                                   r"\ZakKemble.avr-gcc*\*\bin")):
        for d in glob.glob(pat):
            exe = os.path.join(d, name + ".exe")
            if os.path.exists(exe):
                return exe
    sys.exit("%s not found" % name)


def run(*cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit("%s\n%s%s" % (" ".join(cmd), r.stdout, r.stderr))
    return r.stdout


def crc16(data):
    """CRC-16/ARC, the trailer of every application image."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def defines(header):
    out = {}
    for m in re.finditer(r"^#define\s+(\w+)\s+(0x[0-9a-fA-F]+|\d+)\b", open(header).read(), re.M):
        out[m.group(1)] = int(m.group(2), 0)
    return out


def thumb_bl(at, target):
    off = target - (at + 4)
    assert -(1 << 24) <= off < (1 << 24) and not off & 1
    s = (off >> 24) & 1
    j1 = (~((off >> 23) ^ s)) & 1
    j2 = (~((off >> 22) ^ s)) & 1
    hi = 0xF000 | (s << 10) | ((off >> 12) & 0x3FF)
    lo = 0xD000 | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hi, lo)


def build_arm(name):
    t = ARM_TARGETS[name]
    base = t["base"]
    header = os.path.join(SRC, t["header"])
    d = defines(header)
    img = bytearray(open(os.path.join(ROOT, t["image"]), "rb").read())
    if crc16(img[:-2]) != struct.unpack("<H", img[-2:])[0]:
        sys.exit("%s: not a stock image (bad checksum)" % t["image"])
    del img[-2:]
    while len(img) % 4:
        img.append(0xFF)
    load = base + len(img)

    os.makedirs(OUT, exist_ok=True)
    elf = os.path.join(OUT, name + ".elf")
    blob = os.path.join(OUT, name + ".blob")
    ld = os.path.join(OUT, name + ".ld")
    with open(ld, "w") as f:
        f.write("SECTIONS { . = 0x%x; .text : { *(.text*) *(.rodata*) }\n"
                " /DISCARD/ : { *(.comment) *(.ARM.*) *(.note*) } }\n" % load)
    run(tool("arm-none-eabi-gcc"), "-mcpu=cortex-m4", "-mthumb", "-Os", "-ffreestanding",
        "-nostdlib", "-fno-builtin", "-fno-tree-loop-distribute-patterns", "-fno-jump-tables",
        "-Wall", "-Werror", '-DTARGET_H="%s"' % t["header"], "-I", SRC,
        "-Wl,-T," + ld, "-Wl,--build-id=none", "-o", elf, os.path.join(SRC, "find_iems.c"))
    sizes = run(tool("arm-none-eabi-size"), "-A", elf)
    for sec in (".data", ".bss"):
        m = re.search(r"^%s\s+(\d+)" % re.escape(sec), sizes, re.M)
        if m and int(m.group(1)):
            sys.exit("patch must not have a %s section" % sec)
    run(tool("arm-none-eabi-objcopy"), "-O", "binary", "-j", ".text", elf, blob)
    sym = {}
    for line in run(tool("arm-none-eabi-nm"), elf).splitlines():
        addr, _, n = line.split()
        sym[n] = int(addr, 16)
    code = open(blob, "rb").read()

    def at(addr, size):
        return bytes(img[addr - base:addr - base + size])

    for slot, hook in VTABLE_HOOKS:
        a = d["RADIO_VTABLE"] + d[slot]
        want = struct.pack("<I", d[VT_ORIG[slot]] | 1)
        if at(a, 4) != want:
            sys.exit("%s: vtable slot %s holds %s" % (name, slot, at(a, 4).hex()))
        img[a - base:a - base + 4] = struct.pack("<I", sym[hook] | 1)
    for site, orig, hook in CALL_HOOKS:
        a = d[site]
        if at(a, 4) != thumb_bl(a, d[orig]):
            sys.exit("%s: unexpected code at %s" % (name, site))
        img[a - base:a - base + 4] = thumb_bl(a, sym[hook] & ~1)

    cosmetics.patch_logo(img, d["LOGO_BITMAP"] - base)
    cosmetics.patch_main_menu_title(img, d["TITLE_MAIN_MENU"] - base)

    img += code
    img += struct.pack("<H", crc16(img))
    out = os.path.join(HERE, t["image"])
    open(out, "wb").write(img)
    print("%s: patch %d bytes at 0x%x, image %d bytes -> %s" % (
        name, len(code), load, len(img), os.path.relpath(out, ROOT)))


def avr_long(op, target):
    """Encode a 4-byte AVR jmp (op 0x940C) or call (op 0x940E)."""
    k = target >> 1
    return struct.pack("<HH", op | ((k >> 17) & 0x1F) << 4 | (k >> 16) & 1, k & 0xFFFF)


def crc_fixup(img, at, want):
    """Choose img[at:at+2] so that crc16(img[:-2]) == want.  The CRC has no
    initial value or final XOR, so it is linear in the message bits."""
    img[at:at + 2] = b"\0\0"
    need = crc16(img[:-2]) ^ want
    tail = bytes(len(img) - 2 - at - 2)
    basis = [crc16(struct.pack("<H", 1 << bit) + tail) for bit in range(16)]
    for fix in range(0x10000):
        c = 0
        for bit in range(16):
            if fix >> bit & 1:
                c ^= basis[bit]
        if c == need:
            img[at:at + 2] = struct.pack("<H", fix)
            return
    sys.exit("could not fix up the checksum")


def build_p10r():
    name, image = "p10r", "App_P10R.bin"
    d = defines(os.path.join(SRC, "target_p10r.h"))
    img = bytearray(open(os.path.join(ROOT, image), "rb").read())
    trailer = struct.unpack("<H", img[-2:])[0]
    if crc16(img[:-2]) != trailer:
        sys.exit("%s: not a stock image (bad checksum)" % image)
    load = d["MOD_FLASH"]

    os.makedirs(OUT, exist_ok=True)
    elf = os.path.join(OUT, name + ".elf")
    blob = os.path.join(OUT, name + ".blob")
    # Y (r28/r29) is the firmware's data stack pointer: keep gcc away from it
    run(tool("avr-gcc"), "-mmcu=atxmega256a3", "-Os", "-ffixed-r28", "-ffixed-r29",
        "-ffreestanding", "-fno-jump-tables", "-fno-tree-loop-distribute-patterns",
        "-Wall", "-Werror", "-nostartfiles", "-nodefaultlibs", "-P", "-I", SRC,
        "-Wl,--section-start=.text=0x%x" % load, "-o", elf,
        os.path.join(SRC, "glue_p10r.S"), os.path.join(SRC, "find_iems_p10r.c"), "-lgcc")
    sizes = run(tool("avr-size"), "-A", elf)
    for sec in (".data", ".bss", ".rodata"):
        m = re.search(r"^%s\s+(\d+)" % re.escape(sec), sizes, re.M)
        if m and int(m.group(1)):
            sys.exit("patch must not have a %s section" % sec)
    func = None
    for line in run(tool("avr-objdump"), "-d", elf).splitlines():
        m = re.match(r"[0-9a-f]+ <(\w+)>:", line)
        if m:
            func = m.group(1)
        elif re.search(r"[^a-z0-9]r2[89]([^0-9]|$)", line) and func not in ("fw_text_far", "hook_main_menu"):
            sys.exit("p10r: %s uses Y, restructure it: %s" % (func, line.strip()))
    run(tool("avr-objcopy"), "-O", "binary", "-j", ".text", elf, blob)
    sym = {}
    for line in run(tool("avr-nm"), elf).splitlines():
        parts = line.split()
        if len(parts) == 3:
            sym[parts[2]] = int(parts[0], 16)
    code = open(blob, "rb").read()
    end = load + len(code)
    if any(img[load:end + 2]) or end + 2 > len(img) - 0x40:
        sys.exit("p10r: no room for the patch at 0x%x" % load)
    img[load:end] = code

    CALL, JMP = 0x940E, 0x940C
    for addr, old_op, old, new_op, hook in (
            (d["FW_SM_EVENT"], CALL, d["FW_SM_EVENT_PROLOGUE"], JMP, "hook_sm_event"),
            (d["SITE_DRAW_STATE"], CALL, d["FW_SM_STATE"], CALL, "hook_draw_state"),
            (d["SITE_EVENT_POLL"], CALL, d["FW_EVENT_POLL"], CALL, "hook_poll"),
            (d["SITE_MAIN_MENU_TITLE"], CALL, d["FW_MENU_DRAW"], CALL, "hook_main_menu")):
        if bytes(img[addr:addr + 4]) != avr_long(old_op, old):
            sys.exit("p10r: unexpected code at 0x%x" % addr)
        img[addr:addr + 4] = avr_long(new_op, sym[hook])

    cosmetics.patch_logo(img, d["LOGO_BITMAP"])

    # The image keeps its size, and its last two bytes (C0 DE) are both a
    # fixed mark and the checksum: make the checksum come out the same
    # again with two spare bytes after the patch.
    crc_fixup(img, end, trailer)
    assert crc16(img[:-2]) == trailer
    out = os.path.join(HERE, image)
    open(out, "wb").write(img)
    print("%s: patch %d bytes at 0x%x, image %d bytes -> %s" % (
        name, len(code), load, len(img), os.path.relpath(out, ROOT)))


def main(argv):
    for name in argv or list(ARM_TARGETS) + ["p10r"]:
        if name in ARM_TARGETS:
            build_arm(name)
        elif name == "p10r":
            build_p10r()
        else:
            sys.exit("unknown target %s" % name)


if __name__ == "__main__":
    main(sys.argv[1:])
