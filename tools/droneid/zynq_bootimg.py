#!/usr/bin/env python3
"""Take a Zynq-7000 BOOT.bin apart, read the FSBL's ps7_init tables, and put a
new BOOT.bin together from parts of two others. Pure Python, no bootgen.

    # run from: anywhere
    tools/droneid/zynq_bootimg.py info    BOOT.bin
    tools/droneid/zynq_bootimg.py split   BOOT.bin OUTDIR
    tools/droneid/zynq_bootimg.py ps7     BOOT.bin                 # what the FSBL sets up
    tools/droneid/zynq_bootimg.py ps7diff A/BOOT.bin B/BOOT.bin    # where two FSBLs disagree
    tools/droneid/zynq_bootimg.py graft   OUT.bin --fsbl A/BOOT.bin --bit B/BOOT.bin --uboot A/BOOT.bin

WHY IT EXISTS. The MicroPhase DroneID image (built for the ANTSDR E200) lights
DONE on a PlutoSky and then says nothing. The questions that decide what to do
next - is it the DDR set-up, the UART pins, or the clocks the bitstream is fed -
are all answered by ps7_init, which is compiled into the FSBL as a table of
register writes. `ps7diff` reads that table out of both FSBLs and compares it
register by register, so the answer comes from the binaries rather than a guess.

`graft` writes a BOOT.bin whose FSBL, bitstream and U-Boot each come from
whichever image you name. The --fsbl image is the donor: everything bootgen
wrote up to and including its FSBL is kept byte for byte, and only the
partition header table is rewritten. Grafting an image onto itself reproduces
it byte for byte (checked on both the factory and the MicroPhase BOOT.bin), and
the result reads back cleanly with `bootgen -arch zynq -read`.

DO NOT put a graft carrying the MicroPhase bitstream on a PlutoSky: that
bitstream drives nine balls this board's AD9361 drives too. See
docs/droneid-microphase.md, and tools/droneid/pinmap_compare.py for the list.

Exit 0 on success, 1 on a malformed image, 2 on a usage error.
"""
import argparse
import hashlib
import os
import struct
import sys

WIDTH_DETECT = 0xAA995566
XNLX = 0x584C4E58          # "XNLX" little-endian
ATTR_DEST_PL = 0x20        # attribute bits [5:4]: 2 = bitstream, 1 = code for the PS
ATTR_DEST_PS = 0x10


def u32(b, off):
    return struct.unpack_from("<I", b, off)[0]


def checksum(words):
    return (~sum(words)) & 0xFFFFFFFF


class Partition:
    def __init__(self, hdr, data, name):
        (self.enc_len, self.unenc_len, self.total_len, self.load, self.exec_,
         self.offset, self.attr, self.sections, self.cksum_off,
         self.img_hdr_off, self.ac_off) = struct.unpack_from("<11I", hdr)
        self.hdr = hdr
        self.data = data
        self.name = name

    @property
    def kind(self):
        d = self.attr & 0x30
        return "bitstream" if d == ATTR_DEST_PL else "ps" if d == ATTR_DEST_PS else "none"


class BootImage:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.raw = f.read()
        b = self.raw
        if len(b) < 0x8C0 or u32(b, 0x20) != WIDTH_DETECT or u32(b, 0x24) != XNLX:
            raise ValueError(f"{path}: not a Zynq-7000 boot image (no AA995566/XNLX)")
        self.path = path
        self.fsbl_off = u32(b, 0x30)
        self.fsbl_len = u32(b, 0x34)
        self.fsbl_total = u32(b, 0x40)
        hcs = checksum([u32(b, o) for o in range(0x20, 0x48, 4)])
        self.header_ok = hcs == u32(b, 0x48)
        # Register-init table, 256 address/value pairs at 0xA0, terminated by 0xFFFFFFFF.
        self.reginit = []
        for o in range(0xA0, 0x8A0, 8):
            a, v = u32(b, o), u32(b, o + 4)
            if a == 0xFFFFFFFF:
                break
            self.reginit.append((a, v))
        iht = u32(b, 0x98)
        pht = u32(b, 0x9C)
        self.iht_off = iht
        self.pht_off = pht
        count = u32(b, iht + 4)
        names = self._image_names(iht, count)
        self.parts = []
        for i in range(count):
            h = b[pht + 64 * i: pht + 64 * (i + 1)]
            p = Partition(h, None, names[i] if i < len(names) else f"part{i}")
            if checksum(struct.unpack_from("<15I", h)) != u32(h, 60):
                raise ValueError(f"{path}: partition header {i} checksum is wrong")
            start, ln = p.offset * 4, p.total_len * 4
            if start + ln > len(b):
                raise ValueError(f"{path}: partition {p.name} runs past the end of the file")
            p.data = b[start:start + ln]
            self.parts.append(p)

    def _image_names(self, iht, count):
        """Image headers carry the source file's name, big-endian packed words."""
        b, names, off = self.raw, [], u32(self.raw, iht + 12) * 4
        for _ in range(count):
            if not off:
                break
            raw = b""
            o = off + 16
            while True:
                w = b[o:o + 4]
                raw += w[::-1]
                o += 4
                if b"\0" in w:
                    break
            names.append(raw.split(b"\0")[0].decode("ascii", "replace"))
            off = u32(b, off) * 4
        return names

    def part(self, kind):
        if kind == "fsbl":
            return self.parts[0]
        for p in self.parts:
            if kind == "bitstream" and p.kind == "bitstream":
                return p
            if kind == "uboot" and p.kind != "bitstream" and p is not self.parts[0]:
                return p
        raise ValueError(f"{self.path}: no {kind} partition")


# ---------------------------------------------------------------- ps7_init
# ps7_init.c tables, as compiled into the FSBL: each op is a header word
# (opcode << 4 | argument count) followed by its arguments.
OPS = {0x11: ("CLEAR", 1), 0x22: ("WRITE", 2), 0x33: ("MASKWRITE", 3),
       0x42: ("MASKPOLL", 2), 0x52: ("MASKDELAY", 2)}


def plausible_addr(a):
    return (a & 3) == 0 and (0xF8000000 <= a < 0xF9000000 or 0xE0000000 <= a < 0xE0300000)


def ps7_tables(fsbl):
    """[(offset, [(op, addr, mask, value)])] for every ps7_init table found."""
    n = len(fsbl) // 4
    w = struct.unpack_from(f"<{n}I", fsbl)
    tables, i = [], 0
    while i < n:
        ops, j = [], i
        while j < n and w[j] in OPS:
            name, argc = OPS[w[j]]
            args = w[j + 1: j + 1 + argc]
            if len(args) < argc or not plausible_addr(args[0]):
                break
            if name == "MASKWRITE":
                ops.append((name, args[0], args[1], args[2]))
            elif name == "WRITE":
                ops.append((name, args[0], 0xFFFFFFFF, args[1]))
            elif name == "CLEAR":
                ops.append((name, args[0], 0xFFFFFFFF, 0))
            else:
                ops.append((name, args[0], args[1], None))
            j += 1 + argc
        if len(ops) >= 3 and j < n and w[j] == 0:   # EMIT_EXIT()
            tables.append((i * 4, ops))
            i = j + 1
        else:
            i += 1
    return tables


def table_class(ops):
    addrs = {a for _, a, _, _ in ops}
    if 0xF8006000 in addrs:
        return "ddr"
    if 0xF8000700 in addrs or 0xF8000720 in addrs:
        return "mio"
    if 0xF8000100 in addrs:
        return "pll"
    if 0xF8000154 in addrs or 0xF8000170 in addrs:
        return "clock"
    if any(a >> 24 == 0xE0 for a in addrs):
        return "peripherals"
    if any(0xF8800000 <= a < 0xF8900000 for a in addrs) or any(a >= 0xF8890000 for a in addrs):
        return "debug"
    return "post_config"


CLASSES = ("pll", "clock", "ddr", "mio", "peripherals", "post_config", "debug")


def ps7_state(tables):
    """Fold the silicon-3.0 tables into {addr: (mask, value)}: what the FSBL
    leaves behind on a current part.

    An FSBL carries every table three times, for silicon 1.0, 2.0 and 3.0, and
    runs one set. The compiler decides the order they land in the binary (the
    factory FSBL keeps source order, MicroPhase's comes out reversed), so the
    revision is told apart by the DDR table, whose length differs per revision
    in every Xilinx-generated ps7_init.c: 81 ops for 1.0, 83 for 2.0, 82 for 3.0.
    Returns (state, {class: ops}, note)."""
    byclass = {}
    for _, ops in tables:
        byclass.setdefault(table_class(ops), []).append(ops)
    ddr = byclass.get("ddr", [])
    lens = [len(o) for o in ddr]
    note = ""
    if len(ddr) == 3 and sorted(lens) == [81, 82, 83]:
        idx = lens.index(82)
    else:
        idx = 0
        note = f"could not tell silicon revisions apart (DDR tables of {lens} ops); using the first"
    chosen = {}
    for c, lst in byclass.items():
        chosen[c] = lst[idx] if len(lst) == len(ddr) and idx < len(lst) else lst[0]
    state = {}
    for c in CLASSES:
        for op, a, m, v in chosen.get(c, []):
            if v is None:
                continue
            om, ov = state.get(a, (0, 0))
            state[a] = (om | m, (ov & ~m) | (v & m))
    return state, chosen, note


NAMES = {
    0xF8000100: "ARM_PLL_CTRL", 0xF8000104: "DDR_PLL_CTRL", 0xF8000108: "IO_PLL_CTRL",
    0xF8000120: "ARM_CLK_CTRL", 0xF8000124: "DDR_CLK_CTRL", 0xF8000128: "DCI_CLK_CTRL",
    0xF800012C: "APER_CLK_CTRL", 0xF8000138: "GEM0_RCLK_CTRL", 0xF8000140: "GEM0_CLK_CTRL",
    0xF8000148: "SMC_CLK_CTRL", 0xF800014C: "LQSPI_CLK_CTRL", 0xF8000150: "SDIO_CLK_CTRL",
    0xF8000154: "UART_CLK_CTRL", 0xF8000158: "SPI_CLK_CTRL", 0xF800015C: "CAN_CLK_CTRL",
    0xF8000168: "PCAP_CLK_CTRL",
    0xF8000170: "FPGA0_CLK_CTRL (FCLK0)", 0xF8000180: "FPGA1_CLK_CTRL (FCLK1)",
    0xF8000190: "FPGA2_CLK_CTRL (FCLK2)", 0xF80001A0: "FPGA3_CLK_CTRL (FCLK3)",
    0xF80001C4: "CLK_621_TRUE", 0xF8000240: "FPGA_RST_CTRL", 0xF8000900: "LVL_SHFTR_EN",
    0xF8000830: "SD0_WP_CD_SEL", 0xF8000834: "SD1_WP_CD_SEL",
    0xF8000B40: "DDRIOB_ADDR0", 0xF8000B44: "DDRIOB_ADDR1", 0xF8000B48: "DDRIOB_DATA0",
    0xF8000B4C: "DDRIOB_DATA1", 0xF8000B50: "DDRIOB_DIFF0", 0xF8000B54: "DDRIOB_DIFF1",
    0xF8000B58: "DDRIOB_CLOCK", 0xF8000B5C: "DDRIOB_DRIVE_SLEW_ADDR",
    0xF8000B60: "DDRIOB_DRIVE_SLEW_DATA", 0xF8000B64: "DDRIOB_DRIVE_SLEW_DIFF",
    0xF8000B68: "DDRIOB_DRIVE_SLEW_CLOCK", 0xF8000B6C: "DDRIOB_DDR_CTRL",
    0xF8000B70: "DDRIOB_DCI_CTRL",
    0xF8006000: "ddrc_ctrl", 0xF8006004: "Two_rank_cfg", 0xF8006014: "DRAM_param_reg0",
    0xF8006018: "DRAM_param_reg1", 0xF800601C: "DRAM_param_reg2", 0xF8006020: "DRAM_param_reg3",
    0xF8006024: "DRAM_param_reg4", 0xF8006028: "DRAM_init_param", 0xF800602C: "DRAM_EMR_reg",
    0xF8006030: "DRAM_EMR_MR_reg", 0xF8006034: "DRAM_burst8_rdwr",
    0xF800603C: "DRAM_addr_map_bank", 0xF8006040: "DRAM_addr_map_col",
    0xF8006044: "DRAM_addr_map_row", 0xF8006048: "DRAM_ODT_reg",
    0xF8006118: "phy_config0", 0xF800611C: "phy_config1", 0xF8006120: "phy_config2",
    0xF8006124: "phy_config3", 0xF800612C: "phy_init_ratio0", 0xF8006130: "phy_init_ratio1",
    0xF8006134: "phy_init_ratio2", 0xF8006138: "phy_init_ratio3",
    0xF8006140: "phy_rd_dqs_cfg0", 0xF8006144: "phy_rd_dqs_cfg1",
    0xF8006148: "phy_rd_dqs_cfg2", 0xF800614C: "phy_rd_dqs_cfg3",
    0xF8006154: "phy_wr_dqs_cfg0", 0xF8006158: "phy_wr_dqs_cfg1",
    0xF800615C: "phy_wr_dqs_cfg2", 0xF8006160: "phy_wr_dqs_cfg3",
    0xF8006168: "phy_we_cfg0", 0xF800616C: "phy_we_cfg1", 0xF8006170: "phy_we_cfg2",
    0xF8006174: "phy_we_cfg3", 0xF800617C: "wr_data_slv0", 0xF8006180: "wr_data_slv1",
    0xF8006184: "wr_data_slv2", 0xF8006188: "wr_data_slv3",
    0xE0000000: "UART0 Control", 0xE0001000: "UART1 Control",
    0xE0001004: "UART1 Mode", 0xE0000004: "UART0 Mode",
    0xE0001018: "UART1 Baud_rate_gen", 0xE0000018: "UART0 Baud_rate_gen",
    0xE0001034: "UART1 Baud_rate_divider", 0xE0000034: "UART0 Baud_rate_divider",
}
for _i in range(54):
    NAMES[0xF8000700 + 4 * _i] = f"MIO_PIN_{_i:02d}"


def uart_name(pin):
    """UG585 table 2-4: UART1 TX/RX on MIO 8/9, 12/13, ... 52/53 and UART0
    RX/TX on 10/11, 14/15, ... 50/51."""
    if pin % 4 in (0, 1):
        return "UART1 " + ("TX" if pin % 2 == 0 else "RX")
    return "UART0 " + ("RX" if pin % 2 == 0 else "TX")


def mio_func(pin, v):
    """A readable decode of one MIO_PIN register: the function the pin's mux
    selects (UG585 table 2-4, the common cases) and its I/O standard."""
    l0, l1, l2, l3 = (v >> 1) & 1, (v >> 2) & 1, (v >> 3) & 3, (v >> 5) & 7
    io = {1: "LVCMOS18", 2: "LVCMOS25", 3: "LVCMOS33", 4: "HSTL"}.get((v >> 9) & 7, "?")
    if l0:
        f = "Quad-SPI" if pin <= 13 else "Ethernet0" if 16 <= pin <= 27 else \
            "Ethernet1" if 28 <= pin <= 39 else "L0"
    elif l1:
        f = "USB0 ULPI" if 28 <= pin <= 39 else "USB1 ULPI" if 40 <= pin <= 51 else "L1"
    elif l2:
        f = {1: "SRAM/NOR", 2: "NAND", 3: "SDIO/L2"}[l2]
    elif l3 == 7:
        f = uart_name(pin)
    elif l3 == 4:
        f = "MDIO0" if pin in (52, 53) else "SDIO" + ("0" if (pin - 16) % 12 < 6 else "1")
    elif l3 == 5:
        f = "MDIO1" if pin in (52, 53) else "SPI"
    elif l3 == 2:
        f = "I2C" + ("0" if pin % 4 in (2, 3) else "1")
    else:
        f = {0: "GPIO", 1: "CAN", 3: "SWDT", 6: "TTC"}[l3]
    tri = " input" if v & 1 else ""
    pull = " pullup" if (v >> 12) & 1 else ""
    return f"{f} {io}{pull}{tri}"


def describe(addr, val, state):
    """(name, human-readable meaning) of one register value."""
    n = NAMES.get(addr, "")
    if n.startswith("MIO_PIN_"):
        return n, mio_func(int(n[8:]), val)
    if addr == 0xF8006000:
        return n, f"{'32' if (val >> 2) & 3 == 0 else '16'}-bit DDR bus"
    if addr in (0xF8000170, 0xF8000180, 0xF8000190, 0xF80001A0):
        d0, d1, src = (val >> 8) & 0x3F, (val >> 20) & 0x3F, (val >> 4) & 3
        pll = {0: 0xF8000108, 1: 0xF8000108, 2: 0xF8000100, 3: 0xF8000104}[src]
        fd = (state.get(pll, (0, 0))[1] >> 12) & 0x7F
        if d0 and d1 and fd:
            return n, f"{33.333333 * fd / d0 / d1:.2f} MHz (if PS_CLK = 33.333 MHz)"
        return n, ""
    if addr in (0xF8000100, 0xF8000104, 0xF8000108):
        fd = (val >> 12) & 0x7F
        return n, f"x{fd} = {33.333333 * fd:.0f} MHz"
    if addr == 0xF8000154:
        return n, (f"UART0 {'on' if val & 1 else 'off'}, UART1 {'on' if val & 2 else 'off'}, "
                   f"/{(val >> 8) & 0x3F}")
    if addr in (0xF800612C, 0xF8006130, 0xF8006134, 0xF8006138):
        return n, f"wrlvl_init {val & 0x3FF}, gatelvl_init {(val >> 10) & 0x3FF}"
    return n, ""


SECTIONS = (("PLLs and clocks", lambda a: 0xF8000100 <= a < 0xF8000700),
            ("MIO pins", lambda a: 0xF8000700 <= a < 0xF8000800),
            ("SD card detect", lambda a: 0xF8000830 <= a < 0xF8000840),
            ("DDR I/O buffers", lambda a: 0xF8000B00 <= a < 0xF8000C00),
            ("DDR controller and PHY", lambda a: 0xF8006000 <= a < 0xF8007000),
            ("Peripherals (UART, GPIO, ...)", lambda a: a >> 24 == 0xE0),
            ("Other", lambda a: True))


def section(addr):
    for k, (name, test) in enumerate(SECTIONS):
        if test(addr):
            return k
    return len(SECTIONS) - 1


def console_uart(state):
    """Which UART the FSBL clocks, and on which MIO pins, from the ps7 state."""
    clk = state.get(0xF8000154, (0, 0))[1]
    on = [u for u, bit in (("UART0", 1), ("UART1", 2)) if clk & bit]
    pins = sorted(p for p in range(54)
                  if (state.get(0xF8000700 + 4 * p, (0, 0))[1] >> 5) & 7 == 7
                  and not (state.get(0xF8000700 + 4 * p, (0, 0))[1] >> 1) & 0xF)
    return on, [f"MIO{p} {uart_name(p)}" for p in pins]


# ---------------------------------------------------------------- commands
def cmd_info(a):
    img = BootImage(a.image)
    print(f"{a.image}: {len(img.raw)} bytes, boot header checksum "
          f"{'ok' if img.header_ok else 'WRONG'}, {len(img.reginit)} register-init pairs")
    for p in img.parts:
        print(f"  {p.name:<18} {p.kind:<9} offset 0x{p.offset * 4:08x} "
              f"length {p.total_len * 4:>8}  load 0x{p.load:08x}  md5 {hashlib.md5(p.data).hexdigest()}")
    bit = next((p for p in img.parts if p.kind == "bitstream"), None)
    if bit:
        n = bit.total_len * 4
        print(f"  bitstream: {'UNCOMPRESSED (the full XC7Z020 size)' if n > 3_900_000 else 'compressed'}")
        w = struct.unpack_from(f"<{min(len(bit.data), 8192) // 4}I", bit.data)
        ids = [w[k + 1] for k in range(len(w) - 1) if w[k] == 0x30018001]
        if ids:
            dev = {0x03727093: "XC7Z020", 0x03722093: "XC7Z010"}.get(ids[0] & 0x0FFFFFFF, "?")
            print(f"  bitstream IDCODE 0x{ids[0]:08x} ({dev})")
    return 0


def cmd_split(a):
    img = BootImage(a.image)
    os.makedirs(a.outdir, exist_ok=True)
    for i, p in enumerate(img.parts):
        fn = os.path.join(a.outdir, f"{i}-{p.kind}-{os.path.basename(p.name)}.bin")
        with open(fn, "wb") as f:
            f.write(p.data)
        print(f"  {fn}  ({len(p.data)} bytes, load 0x{p.load:08x})")
    return 0


def summary(path):
    img = BootImage(path)
    tables = ps7_tables(img.part("fsbl").data)
    state, chosen, note = ps7_state(tables)
    if note:
        print(f"WARNING: {path}: {note}", file=sys.stderr)
    return img, tables, state, chosen


def cmd_ps7(a):
    _, tables, state, chosen = summary(a.image)
    print(f"{a.image}: {len(tables)} ps7_init tables in the FSBL; silicon 3.0 set: "
          + ", ".join(f"{c} {len(chosen[c])}" for c in CLASSES if c in chosen))
    last = None
    for addr in sorted(state, key=lambda x: (section(x), x)):
        m, v = state[addr]
        n, meaning = describe(addr, v, state)
        if not (a.all or n):
            continue
        if section(addr) != last:
            last = section(addr)
            print(f"\n  -- {SECTIONS[last][0]}")
        print(f"  0x{addr:08x} {n:<24} 0x{v:08x}  {meaning}")
    on, pins = console_uart(state)
    print(f"\n  UART clocked: {', '.join(on) or 'none'};  UART pins: {', '.join(pins) or 'none'}")
    return 0


def cmd_ps7diff(a):
    _, _, sa, ca = summary(a.a)
    _, _, sb, cb = summary(a.b)
    print(f"A = {a.a}\nB = {a.b}")
    counts = {}
    last = None
    for addr in sorted(set(sa) | set(sb), key=lambda x: (section(x), x)):
        va, vb = sa.get(addr), sb.get(addr)
        k = section(addr)
        if va and vb:
            common = va[0] & vb[0]
            if (va[1] & common) == (vb[1] & common):
                continue
        counts[k] = counts.get(k, 0) + 1
        if k != last:
            last = k
            print(f"\n  -- {SECTIONS[k][0]}")
        n, ma = describe(addr, va[1], sa) if va else (NAMES.get(addr, ""), "")
        _, mb = describe(addr, vb[1], sb) if vb else ("", "")
        ta = f"0x{va[1]:08x}" if va else "(not set) "
        tb = f"0x{vb[1]:08x}" if vb else "(not set) "
        print(f"  0x{addr:08x} {n:<20} A {ta} {ma:<34} B {tb} {mb}")
    print("\n  differences per section: " + (", ".join(
        f"{SECTIONS[k][0]} {c}" for k, c in sorted(counts.items())) or "none"))
    for tag, s in (("A", sa), ("B", sb)):
        on, pins = console_uart(s)
        print(f"  {tag}: UART clocked {', '.join(on) or 'none'}; pins {', '.join(pins) or 'none'}")
    ddr = [x for x in set(sa) | set(sb) if 0xF8006000 <= x < 0xF8007000 or 0xF8000B40 <= x < 0xF8000B80]
    same = all(sa.get(x, (0, 0))[1] == sb.get(x, (0, 0))[1] for x in ddr)
    print(f"  DDR set-up (controller, PHY ratios, I/O buffers): {'IDENTICAL' if same else 'DIFFERENT'}")
    return 0


def build_image(donor, parts):
    """A BOOT.bin that is `donor` with every partition after the FSBL replaced.

    Everything bootgen wrote before the FSBL (boot header, register-init table,
    image header table, image headers with their file names) is kept byte for
    byte, so the result is laid out exactly as bootgen lays out the donor. Only
    the partition header table is rewritten: length, offset, load/exec address
    and attributes of each new partition, and each entry's checksum. Partitions
    start on 64-byte boundaries with 0xFF between them, as bootgen does.

    `parts` is [(data, load, exec, attr)], one per donor partition after the FSBL."""
    if len(parts) != len(donor.parts) - 1:
        raise ValueError(f"{donor.path} has {len(donor.parts) - 1} partitions after the FSBL, "
                         f"{len(parts)} given")
    fsbl = donor.parts[0]
    placed = [(fsbl.data, fsbl.load, fsbl.exec_, fsbl.attr, fsbl.offset * 4)]
    cur = fsbl.offset * 4 + len(fsbl.data)
    for data, load, exe, attr in parts:
        if len(data) % 4:
            raise ValueError("a partition must be a whole number of 32-bit words")
        cur += -cur % 64
        placed.append((data, load, exe, attr, cur))
        cur += len(data)
    out = bytearray(b"\xff" * cur)
    out[:fsbl.offset * 4] = donor.raw[:fsbl.offset * 4]
    for i, (data, load, exe, attr, off) in enumerate(placed):
        out[off:off + len(data)] = data
        h = donor.pht_off + 64 * i
        hdr = list(struct.unpack_from("<15I", donor.raw, h))
        wl = len(data) // 4
        hdr[0:7] = [wl, wl, wl, load, exe, off // 4, attr]
        struct.pack_into("<16I", out, h, *hdr, checksum(hdr))
    return bytes(out)


def cmd_graft(a):
    srcs = {"fsbl": BootImage(a.fsbl), "bit": BootImage(a.bit), "uboot": BootImage(a.uboot)}
    bit = srcs["bit"].part("bitstream")
    ub = srcs["uboot"].part("uboot")
    parts = [(bit.data, bit.load, bit.exec_, bit.attr), (ub.data, ub.load, ub.exec_, ub.attr)]
    donor = srcs["fsbl"]
    if [p.kind for p in donor.parts[1:]] != ["bitstream", "ps"]:
        raise ValueError(f"{a.fsbl}: expected FSBL, bitstream, U-Boot")
    out = build_image(srcs["fsbl"], parts)
    with open(a.out, "wb") as f:
        f.write(out)
    chk = BootImage(a.out)
    for kind, src in (("fsbl", srcs["fsbl"]), ("bitstream", srcs["bit"]), ("uboot", srcs["uboot"])):
        if chk.part(kind).data != src.part(kind).data:
            print(f"ERROR: {kind} partition did not survive the round trip", file=sys.stderr)
            return 1
    print(f"wrote {a.out} ({len(out)} bytes, md5 {hashlib.md5(out).hexdigest()})")
    print(f"  FSBL      from {a.fsbl}\n  bitstream from {a.bit}\n  U-Boot    from {a.uboot}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("info"); p.add_argument("image"); p.set_defaults(fn=cmd_info)
    p = sp.add_parser("split"); p.add_argument("image"); p.add_argument("outdir"); p.set_defaults(fn=cmd_split)
    p = sp.add_parser("ps7"); p.add_argument("image"); p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_ps7)
    p = sp.add_parser("ps7diff"); p.add_argument("a"); p.add_argument("b")
    p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_ps7diff)
    p = sp.add_parser("graft"); p.add_argument("out")
    for k in ("--fsbl", "--bit", "--uboot"):
        p.add_argument(k, required=True)
    p.set_defaults(fn=cmd_graft)
    a = ap.parse_args()
    try:
        return a.fn(a)
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
