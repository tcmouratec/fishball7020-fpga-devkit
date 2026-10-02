#!/usr/bin/env python3
"""Compare two Vivado pin maps (.xdc) ball by ball, and flag the balls where a
bitstream built for board B would DRIVE a net that board A's other chips drive.

    # run from: anywhere
    tools/droneid/pinmap_compare.py PLUTOSKY.xdc E200.xdc

    PLUTOSKY.xdc  the board you have: Fish-Wan-plutosdr-fw-7020-SDR's
                  hdl/projects/pluto/system_constr.xdc (what firmware/ builds)
    E200.xdc      the board the bitstream was built for: MicroPhase's
                  antsdr-fw-patch, hdl/projects/e200/system_constr.xdc

WHY IT EXISTS. "The FPGA configures, DONE lights" says nothing about whether a
bitstream belongs on this board. A Zynq bitstream fixes every pin's direction;
loading another board's onto this one turns the FPGA into a driver on whatever
traces happen to reach those balls. This prints, per ball, what each board uses
it for, whether the foreign bitstream drives it, and whether this board has
something else driving the same trace (the AD9361's outputs): two outputs on one
net is contention, the case that can damage a pin.

Directions come from the port names (ADI's axi_ad9361 conventions plus the E200's
own ports): *_in, *_status, *miso, rx_* are inputs; everything else that is not
obviously bidirectional is treated as an output. Exit 0, or 1 if any ball is
contended.
"""
import re
import sys

PIN = re.compile(r"PACKAGE_PIN\s+(\w+).*?get_ports\s+\{?([\w\[\]]+)\}?")


def read(path):
    m = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#"):
                continue
            g = PIN.search(line)
            if g:
                port = g.group(2)
                if port.count("]") > port.count("["):
                    port = port[:-1]
                m[g.group(1)] = port
    return m


def base(port):
    """rx_data_in_p[3] and rx_data_in[3] are the same signal; _p/_n is LVDS."""
    return re.sub(r"_[pn](?=\[|$)", "", port.lower())


def direction(port):
    p = port.lower()
    if p.startswith(("rx_clk_in", "rx_frame_in", "rx_data_in", "gpio_status",
                     "spi_miso", "pl_spi_miso", "rgmii_rd", "rgmii_rx", "pps_in",
                     "clk_40mhz_fpga", "clkin_10mhz[", "emio_uart1_rxd")) or p == "clkin_10mhz":
        return "in"
    if p.startswith(("iic_", "mdio_phy_mdio", "gpiob")):
        return "inout"
    return "out"


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    here, foreign = read(sys.argv[1]), read(sys.argv[2])
    bad = 0
    rows = []
    for ball in sorted(set(here) | set(foreign)):
        a, b = here.get(ball), foreign.get(ball)
        da = direction(a) if a else None
        db = direction(b) if b else None
        if b is None:
            verdict = "not used by the foreign bitstream"
        elif a is None:
            verdict = ("foreign DRIVES a ball this board's design leaves unused - check the schematic"
                       if db != "in" else "foreign only listens")
        elif db == "in":
            verdict = "foreign only listens" + ("" if base(a) == base(b) else " (to a different signal)")
        elif da == "in":
            verdict = "CONTENTION: foreign drives a net this board's AD9361/peripheral drives"
            bad += 1
        elif base(a) == base(b):
            verdict = "same signal"
        else:
            verdict = "foreign drives a DIFFERENT signal onto this net"
        rows.append((ball, a or "-", da or "", b or "-", db or "", verdict))
    w = max(len(r[1]) for r in rows)
    print(f"{'ball':<5} {'this board':<{w}} dir   {'foreign bitstream':<22} dir    verdict")
    for ball, a, da, b, db, v in rows:
        print(f"{ball:<5} {a:<{w}} {da:<5} {b:<22} {db:<6} {v}")
    print(f"\n{len(rows)} balls, {bad} contended")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
