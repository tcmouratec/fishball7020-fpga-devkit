# The MicroPhase DroneID image on a PlutoSky: why it is silent, and why it cannot work

MicroPhase publishes a DJI DroneID detector for its **ANTSDR E200** as five SD-card
files (`build_sdimg_*.zip`). The E200 is a Zynq XC7Z020-CLG400 plus an AD936x, as is
this board. On a PlutoSky R1 the image configures the FPGA (the DONE LED lights)
and then shows nothing: no serial output on either FT2232H channel, no USB gadget,
no heartbeat. The obvious guess is that the FSBL's DDR set-up does not suit this
PCB, and that replacing the FSBL would fix it.

The binaries say otherwise:

- **DDR works.** The silence comes from pin multiplexing: the console, USB and
  Ethernet are all routed to pins that are not wired to anything on this board.
- **Fixing the boot would not help.** The E200's PCB connects the AD9361 to
  **different FPGA balls**, in CMOS mode rather than LVDS. MicroPhase's
  bitstream therefore cannot reach this board's radio. Worse, it drives nine
  balls that this board's AD9361 drives too.
- **The decoders need MicroPhase's FPGA logic.** Both decoders open a custom DMA
  device that exists only in that bitstream. Copying them onto this board's own
  firmware does not work either.

**Do not boot the MicroPhase image on a PlutoSky again**, and do not build a
`BOOT.bin` that carries its bitstream. The viable route is the last section:
run this board's own firmware as the receiver and decode on the host.

Everything below can be reproduced offline with two scripts in
[`tools/droneid/`](../tools/droneid/README.md), and the commands are given with
each finding.

## What was compared

| | Source | Notes |
|---|---|---|
| MicroPhase image | `build_sdimg_drone_net.zip` in [alphafox02/antsdr_dji_droneid](https://github.com/alphafox02/antsdr_dji_droneid) | Its `devicetree.dtb` (md5 `a9543ff5…`) and `uEnv.txt` (md5 `f492fc67…`) are **identical** to the `build_sdimg_DG.zip` image tested on hardware. Its `BOOT.bin` is a different build, so re-run `ps7diff` on the DG `BOOT.bin` to confirm (one command, below) |
| MicroPhase O4 image | `build_sdimg_drone_o4.zip`, same repository | Same FSBL DDR set-up and console pins as the image above |
| Factory firmware | [v1.7 release](https://github.com/matsvandamme/fishball7020-fpga-devkit/releases/tag/v1.7) `BOOT.bin`, `devicetree.dtb`, `system_top.xsa` | Its `ps7_init.c` (inside the XSA) was used to check the decoder |
| E200 pin map | [MicroPhase/antsdr-fw-patch](https://github.com/MicroPhase/antsdr-fw-patch), `patch/0001-add-three-ant-hdl-support-bump-to-v0.39-v1.patch`, file `projects/e200/system_constr.xdc` | MicroPhase's own HDL project for the E200 |
| PlutoSky pin map | `hdl/projects/pluto/system_constr.xdc` at the upstream commit `firmware/` pins (`95aad369`) | What this repository builds |

## 1. DONE proves that DDR works

When the FSBL boots from an SD card, it does not program the FPGA straight from
the card. It first copies the bitstream into DDR, at `DDR_TEMP_START_ADDR`, and
then hands it to the configuration port from there (AMD embeddedsw
`zynq_fsbl/src/image_mover.c`, `PartitionMove`: "PL partition copied to DDR
temporary location"). The DONE pin rises only after a complete, CRC-correct
bitstream has arrived. So DONE means that `ps7_init`, DDR set-up included, ran
to completion and that DDR held 4 MB without corruption.

The FSBLs do configure DDR differently, but not in a way that stops anything:

```
$ tools/droneid/zynq_bootimg.py ps7diff factory/BOOT.bin microphase/BOOT.bin
  0xf8006000 ddrc_ctrl   A 0x00000081 32-bit DDR bus   B 0x00000085 16-bit DDR bus
  ... (address map, two PHY lanes disabled)
```

The E200 FSBL sets up a 16-bit bus, which means one x16 DDR3 chip. The PlutoSky
has two (`U2`, `U3`, 32 bits, 1 GB). In 16-bit mode the controller simply uses the first chip and leaves the
second idle: 512 MB that work. The PHY's write-levelling and gate-training
ratios (`phy_init_ratio*`, `phy_*_cfg*`) are **identical** in the two FSBLs.

## 2. Why nothing comes out of either serial port

| | FSBL pin mux and clock | U-Boot console | Linux console |
|---|---|---|---|
| **PlutoSky** (factory) | UART1 on **MIO 8/9**, the pins wired to the FT2232H | `serial@e0001000` (UART1) | `ttyPS0` = UART1 |
| **MicroPhase** image | UART0 on **MIO 14/15**; UART1 clocked but on no MIO pin | `stdout-path = serial@e0000000` (UART0); the built-in tree reads `model = "MicroPhase, ANTSDR-E200"` | `serial0` = UART1, routed through EMIO into the fabric, to balls **Y9/Y6** in the E200 design |

From `ps7diff`:

```
  A: UART clocked UART1; pins MIO8 UART1 TX, MIO9 UART1 RX
  B: UART clocked UART0, UART1; pins MIO14 UART0 RX, MIO15 UART0 TX
```

U-Boot writes to MIO 14/15 and Linux writes to fabric balls Y9/Y6, and on this
board neither reaches `ttyUSB0` or `ttyUSB1`. Silence at every baud rate is
exactly what this predicts, and it says nothing about whether the processor
is running. (`drone_uart_serial` sends its output to `/dev/ttyPS1`, the
other UART.)

The other missing signs of life follow from the same table:

- **USB**: the PlutoSky's USB3320C PHY sits on MIO 28–39 (USB0 ULPI). The
  MicroPhase FSBL leaves those pins as GPIO, and its tree sets the controller
  to `dr_mode = "host"`. No gadget can enumerate, so `192.168.2.1` cannot appear.
- **Ethernet**: the PlutoSky's RTL8211F is on MIO 16–27 plus MDIO on 52/53. The
  E200 runs its PHY through the fabric (`gmii-to-rgmii` on balls C20…H16), and
  the MicroPhase FSBL leaves MIO 16–27 as GPIO. Wired Ethernet cannot come up either.
- **MIO voltage**: the MicroPhase FSBL declares bank 501 (MIO 16–53) as
  LVCMOS33, while this board powers that bank at 1.8 V.
- **Heartbeat**: both trees put `led0` on MIO 0, so the LED's own pin is not the
  problem. A heartbeat that never starts suggests Linux did not get as far as
  the LED driver. Without a console nothing more can be learned, and section 3
  makes the question moot.

## 3. The blocker: MicroPhase's bitstream does not fit this board

The FPGA's balls are fixed by the bitstream. Comparing the two pin maps ball by ball:

```
$ tools/droneid/pinmap_compare.py plutosky_constr.xdc e200_constr.xdc
...
96 balls, 9 contended
```

| | PlutoSky | ANTSDR E200 |
|---|---|---|
| AD9361 data interface | **LVDS**, 6 pairs each way, `LVDS_25` | **CMOS**, 12 bits each way, `LVCMOS18` |
| `gpio_resetb` | R19 | T17 |
| `spi_clk` / `spi_mosi` / `spi_miso` / `spi_csn` | V18 / P16 / V17 / R17 | R19 / P18 / T19 / T20 |
| `enable` / `txnrx` / `gpio_en_agc` | T15 / P18 / P20 | R18 / N17 / P16 |
| `rx_clk_in` | U18/U19 (pair) | N20 |

MicroPhase's devicetree agrees: it has no `adi,lvds-mode-enable`, so the
driver would program the AD9361 for CMOS even if it could reach the chip.

On this board MicroPhase's bitstream therefore:

- clocks **R19**, this board's AD9361 `RESETB`, as an SPI clock, and drives
  **P18** (`TXNRX` here) with SPI data. The driver can never configure the
  radio, because the radio's SPI pins (V18/P16/V17/R17) are wired as inputs or
  left unused.
- **drives nine balls that are outputs of this board's AD9361**:

| Ball | Driven by the AD9361 on the PlutoSky | Driven by MicroPhase's bitstream as |
|---|---|---|
| U18, U19 | `rx_clk_in_p/n` (LVDS data clock) | `tx_data_out[3]`, `tx_data_out[2]` |
| T16, U17 | `rx_data_in_p/n[1]` | `tx_data_out[1]`, `tx_data_out[0]` |
| T17, R18 | `rx_data_in_p/n[3]` | `gpio_resetb`, `enable` |
| T20 | `rx_data_in_p[4]` | `spi_csn` |
| T14 | `gpio_status[3]` (CTRL_OUT) | `gpio_ctl[1]` |
| M20 | `gpio_status[5]` (CTRL_OUT) | `CLKIN_10MHz_REQ` |

Two outputs on one trace is contention. While the AD9361 is held in reset its
outputs may well be high-impedance, and R19 is toggled rather than held, so
this may have done no harm. It is still not a configuration to run for hours.
The bitstream also drives balls this board's design leaves unused (the E200's
RGMII and GPIOB, among them V10/U10/T9 on the JP5 header). What those connect
to on the PlutoSky needs the schematic.

**Check the radio after the two test boots.** Return to the factory or modern
firmware and run the self-test. It is read-only, never transmits, and measures
the very lines in the table above through the digital interface eye:

```bash
# run from: the repo root
./devkit selftest
```

A healthy board passes **157–158 of 256** delay positions
([measured performance](measured-performance.md)). A narrower eye, or a failed digital loopback, points
at the RX LVDS lines.

## 4. What this means for the plans

**Plan A (replace the FSBL)** would at best give a console: a factory FSBL
routes UART1 to MIO 8/9, and Linux's `ttyPS0` would then appear. MicroPhase's
U-Boot would still write to UART0, and the radio still could not work, as
section 3 shows. It also keeps the nine contended balls. `zynq_bootimg.py graft`
can build such an image and `bootgen` reads it back cleanly, but **do not
flash it**.

**Plan B (move the application onto this board's firmware)** fails on the FPGA
dependency, as the briefing suspected, and the dependency is larger than the
briefing assumed:

| Binary | Needs |
|---|---|
| `done_dji_release` (legacy, binary frames on TCP 41030) | `/dev/my-axi-droneid-filter0` (IP at `0x43c00000`, `xlnx,axi-droneid-filter-1.0`) **and** `/dev/my-axi-dma-driver_00` (AXI DMA at `0x40400000`, `xlnx,my-axi-dma-wrapper-1.0`, out-of-tree kernel module), plus libiio, `libfftw3f`, `libserialport` |
| `drone_dji_rid_decode` (O4 image, CSV on TCP 52002) | `/dev/my-axi-dma-driver_00` and **`/dev/mem`** (direct register access), plus the same libraries |

Neither will run on this board's bitstream. The custom IP's HDL is not
public, and recovering it from the bitstream would mean reverse engineering
the netlist. Starting `done_dji_release` with no device present is harmless
(it prints `open_device device Failed`), but it will not decode anything.

## 5. The route that can work: this board as the receiver, decoding on the host

This board's own firmware already streams IQ over Gigabit Ethernet, and the
DroneID signal format is public. Open-source decoders that take raw IQ include
[RUB-SysSec/DroneSecurity](https://github.com/RUB-SysSec/DroneSecurity) (from the
NDSS 2023 paper that reverse-engineered DroneID) and
[proto17/dji_droneid](https://github.com/proto17/dji_droneid). Run one of them on
the Raspberry Pi 5, and publish its output in the JSON that `dji_receiver.py`
puts on ZMQ port 4221. DragonSync and TAK then work unchanged.

What to measure first, before writing the glue:

- **Sample rate.** DroneID bursts are about 10 MHz wide and the decoders expect
  15.36 MSPS. [Throughput](modulation-and-throughput.md) measured about **11.3 MS/s**
  sustained for one receive channel at a 1 Msample buffer, over a WiFi-limited host.
  The Pi 5's wired port may do better, so measure it. If the link cannot keep
  up, libiio drops whole buffers but each buffer stays contiguous, and a
  1 Msample buffer at 15.36 MSPS covers 68 ms. A DroneID frame lasts under 1 ms and
  repeats, so a stream with gaps should still catch frames, only fewer of
  them. That is a hypothesis to measure, not a result.
- **If that is not enough**: on the board itself, capture reaches 183–220 MB/s.
  A burst detector on the board's ARM (or in the fabric, using
  [the block design](block-design.md) and its existing decimation path) could
  forward only the bursts. That is the same split MicroPhase uses, built from
  parts this repository can rebuild.
- **O4** (DJI Mini 5 and later) is encrypted. MicroPhase's O4 decoder reports
  only a hash, frequency and RSSI, and an open decoder will do no better.

## Reproducing this

```bash
# run from: the repo root, with the five MicroPhase files in mp/ and the
# factory release's BOOT.bin in factory/
tools/droneid/zynq_bootimg.py info    mp/BOOT.bin           # partitions, bitstream size, IDCODE
tools/droneid/zynq_bootimg.py ps7diff factory/BOOT.bin mp/BOOT.bin
tools/droneid/zynq_bootimg.py split   mp/BOOT.bin mp-parts  # fsbl, bitstream, u-boot
dtc -I dtb -O dts mp/devicetree.dtb | grep -nE 'lvds|serial0|stdout|droneid|dma@'
tools/droneid/pinmap_compare.py plutosky_constr.xdc e200_constr.xdc
```

`ps7diff` needs nothing but Python 3. It reads the `ps7_init` register tables
compiled into each FSBL, picks the silicon-3.0 set and compares the result
register by register. It was checked against the factory XSA's own
`ps7_init.c`.
