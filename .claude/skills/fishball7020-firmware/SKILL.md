---
name: fishball7020-firmware
description: Build, flash, measure and safely transmit with the Fishball7020 / PlutoSky SDR (Zynq XC7Z020 + AD9361, sold also as PlutoSky R1 and 7020-SDR). Use for FPGA and HDL changes, Vivado block-design work, kernel and device-tree patches, BOOT.bin and bitstreams, flashing, libiio/iiod and sysfs access, IQ capture, transmitting, RF loopback measurement, AD9361 gain tables and ENSM, TX muting, and diagnosing a board that misbehaves. Encodes rules that are expensive to rediscover - flash only via the SD partition and never DFU, delete the Vivado project before an HDL change or the build silently reuses the old one, simulate before synthesising, never loop TX to RX without at least 20 dB of attenuation, and find out which of the two userspaces the board is running before believing anything about it.
license: GPL-2.0
compatibility: Board reached over its USB Ethernet gadget (default ip:192.168.2.1). U-Boot/kernel builds need an ARM Linux cross-compiler, `gcc-arm-linux-gnueabi` (preferred for the factory target; the only one that rebuilds the factory kernel byte for byte) or `arm-linux-gnueabihf-gcc` (both targets accept it; U-Boot is built with `-mfloat-abi=soft`); `./devkit container` provides gnueabi. Buildroot is needed only for the factory root filesystem. HDL builds need Vivado 2022.2 and nothing else from AMD (no xsct/Vitis: the FSBL builds from AMD's embeddedsw with gcc-arm-none-eabi, and setup.sh builds bootgen from AMD's Apache-2.0 source), so a --xsa build needs no AMD tool installed at all; HDL simulation needs only iverilog; the host tools need Python 3.8, plus sshpass for anything that reaches the board over ssh (flash, selftest --ssh, gpio-check, net, verify --board).
metadata:
  repository: fishball7020-fpga-devkit
  board: Fishball7020 / PlutoSky R1 (XC7Z020 + AD9361)
---

# Working on the Fishball7020 / PlutoSky

A reverse-engineered, buildable firmware for a board sold under several names:
Zynq XC7Z020 + AD9361, two transmit and two receive chains, and on the common
variant **a power amplifier (PA)**, which breaks the safety arithmetic most
Pluto advice assumes.

**Two firmware targets; pick the right one before you start.**

| | `firmware/` (factory) | `firmware-modern/` (modern) |
|---|---|---|
| Linux | 5.15.0, the vendor's fork of a fork | **6.12.0 LTS, Analog Devices' `main`** |
| created by | `./devkit setup` | `./firmware-modern/setup.sh` |
| kernel source | `firmware/src/linux` (a monorepo with U-Boot and Buildroot beside it) | `firmware-modern/src/linux` (just the kernel) |
| device tree | `arch/arm/boot/dts/zynq-pluto-sdr-fishball.dts`, flat, 1003 lines | `arch/arm/boot/dts/xilinx/…`, an overlay, ~200 lines |
| defconfig | `zynq_pluto_defconfig` | `fishball_defconfig` |
| patches | `firmware/patches/`, 18 | `firmware-modern/patches/`, 9, drivers only |
| root filesystem | Buildroot/busybox on a RAM disk | Debian 13 trixie with systemd, on an ext4 partition |
| CI | `verify-patches.yml` | `verify-modern.yml` |

Default to `firmware-modern/` for anything kernel- or driver-related.
`firmware/` exists because the byte-identical factory claim is only meaningful
against the factory kernel. **The bitstream, `BOOT.bin` and U-Boot are shared;
the root filesystem is NOT.** v2.2 is the latest modern release, and the board
currently runs the v2.2 files. A kernel swap is a single file:
`./devkit flash --kernel-only` puts the board back in six seconds with the old
one kept as `uImage.prev`.

**Find out what you are talking to before you trust any rule below.** The two
userspaces differ in ways that turn a correct command into a silent no-op:

```bash
# run on the board
cat /etc/os-release   # Debian GNU/Linux 13 -> firmware-modern/debian
uname -r              # 6.12.0-... or 5.15.0
systemctl is-system-running 2>/dev/null || echo "no systemd - Buildroot"
```

All combinations occur: the 6.12 kernel boots the Buildroot ramdisk, and Debian
boots on either kernel. `fw_setenv rootfs_mode ramdisk` switches userspace
without a card reader.

Every busybox limitation below applies to **Buildroot**. On **Debian** they are
gone: there IS `pkill`, sshd is OpenSSH rather than dropbear, `apt` works, and
the root is a writable ext4 partition rather than a RAM disk. See
[`firmware-modern/debian/README.md`](../../../firmware-modern/debian/README.md),
and [`docs/debian-root-reference.md`](../../../docs/debian-root-reference.md) for
what each boot unit and overlay setting does. Kernel background (why ADI 6.12,
the device tree, the IIO diff against 5.15):
[`docs/modern-kernel.md`](../../../docs/modern-kernel.md).

Depth lives in `references/`; load only what the task needs.

| | |
|---|---|
| [`rf-safety.md`](references/rf-safety.md) | **Read before anything transmits.** The PA, the power budget, TX muting |
| [`build-and-flash.md`](references/build-and-flash.md) | Exact command sequences for building, flashing, recovering |
| [`ad9361-gain-tables.md`](references/ad9361-gain-tables.md) | Why gain in dB is not gain in dB, and where the discontinuities are |
| [`measuring.md`](references/measuring.md) | The self-test, baselines, and what is a property of the board versus the cable |
| [`talking-to-the-board.md`](references/talking-to-the-board.md) | libiio/IIOD, sysfs, debugfs, networking, and what busybox does not have |
| [`debugging.md`](references/debugging.md) | Traps that are slow to diagnose: symptom, cause, fix |

## The rules

**Flash with `./devkit flash`. Never DFU.** DFU has no `BOOT.bin` target, so it
can never deliver an HDL change, and on this board it has bricked units. The
script mounts `/dev/mmcblk0p1` on the running board, backs the card up to
`firmware/.flash-backups/<stamp>/` (gitignored), md5-verifies each copy BEFORE
swapping it in, keeps the old files on the card as `*.prev`, unmounts cleanly,
reboots, and reports success only once `/proc/uptime` has reset and the card
md5s match. `--boot-only` for HDL, `--kernel-only` for a driver change,
`--dtb-only` for a device-tree-only patch (0002/0008/0011), `--all` for a
release. `BOARD` / `BOARD_PASS` override the address and password. A bad
`BOOT.bin` removes this route entirely: recovery is a card reader.

**Delete the Vivado project before any HDL or coefficient change.**
`build_hdl.tcl` reuses an existing `pluto.xpr` rather than re-running
`system_bd.tcl`, so a changed block design or `.coe` is *silently ignored* and
you flash the old bitstream:

```bash
# run from: firmware/
rm -rf src/hdl/projects/pluto/pluto.{xpr,cache,gen,hw,ip_user_files,runs,sim,srcs,sdk}
```

**Four header pins carry the transmit sample's low nibble** (JP5 pins
7/9/11/13). The libgpiod line offsets are 72–75 on every kernel, because that
is a property of the bitstream; the sysfs numbers are `base + 72`, and the base
moves (906 on 5.15, 512 on 6.12, so 978–981 and 584–587). **Resolve the base by
chip label, not with `gpiofind`: libgpiod-tools is NOT installed on the Debian
rootfs**, so the command is not found there. `tools/tx-gpio-bitmap-check.py`
does it portably:
`for g in /sys/class/gpio/gpiochip*; do grep -q zynq_gpio $g/label && cat $g/base; done`.
Enable: `echo 1 > /sys/bus/iio/devices/iio:deviceN/tx_sample_gpio_en` on
`cf-ad9361-dds-core-lpc`; resolve `N` by name, never assume the index. Verify
with `./devkit gpio-check` (no scope, no antenna).
- Pin-to-pin timing: all four within 1.5 ns, every sample present up to
  61.44 MSPS (logic analyser). The pins LEAD the RF by a constant offset of
  roughly a microsecond that is designed-for, not measured: never write "the pin
  edge and its RF happen together".
- **Never engage the FPGA ÷8 TX interpolator** (DAC core rate = AD rate / 8):
  upstream's `tx_upack` read-enable ORs in channel 1's DAC valid on this 2R2T
  board, and TX1 then emits nothing (spectrum = TX muted, within 1.2 dB).
  pyadi-iio and the MCP use the AD9361's own FIR below 2.083 MSPS and never
  touch it.
- Always compare a spectrum against a muted reference in absolute dBFS: a
  normalised spectrum makes silence look like "a spray of components".
- Three ways to fool yourself: a pin read with `direction=out` returns what you
  *wrote*; a pin's level alone never says who drives it (stream two different
  nibbles); and the nibble must be OR-ed into the samples **last**.
- Balls, bank, pull-down, the capture strobe and the resource cost:
  [`docs/tx-gpio-bitmap.md`](../../../docs/tx-gpio-bitmap.md).

**Never test for the board with `ping`.** Use `tools/board_addr.py --check`,
which exits 0 only if the board answers. `reachable()` makes each service
identify itself (`iiod` answers `VERSION`, dropbear names itself in its SSH
banner), so something else on an open port is not mistaken for the board, and
it needs no ICMP. **The build container ships no `ping` at all**, so a ping
check reports "no board" in there however good the address is. The container
also has no mDNS, so a `.local` name cannot resolve inside it:
`tools/container/run.sh` resolves on the host and forwards an address as
`$BOARD` plus `--add-host` for the name.

**`./devkit` is the entry point; `doctor` comes first.** `doctor · setup · sim ·
build · verify · flash · selftest · gpio-check · net · status` (and more, see
`./devkit --help`), all from the repo root with arguments passed through.
`./devkit doctor` checks Vivado, the bare-metal cross-compiler, host packages,
`gmp.h`, disk (~25 GB), the patch stamp and the board in about a second; each
check is a failure that otherwise costs an hour mid-build. `./devkit verify`
before flashing; `./devkit verify --board` after: it md5-compares the card
against `output/` and is the only thing that proves the board runs what you
built. A STALE verdict means the board is behind, not that the build is bad.
`setup.sh` is idempotent (it stamps `src/.devkit-patches-applied` with a digest
of the patch set), and `build_all.sh` refuses an unpatched tree and a Vivado
project older than its sources. **`--board` never changes the exit status**: a
board that is absent, unmountable or stale is a fact about the board, not a
fault in the build, so read the verdict rather than `$?`. `--require-board` is
the strict form for a release gate: it exits non-zero unless the card was
actually read and every file matched.

**TX attenuation and the stream lifecycle: four rules.** Patch 0005 makes a
buffer start restore a *cached* attenuation when the chip looks muted, and the
stop hook snapshots whatever attenuation it finds into that cache *before*
applying maximum. So:
1. **Set TX attenuation AFTER a buffer starts, then read it back.** Writing
   −89.75 dB before opening a buffer guarantees nothing during it. The one
   exception is a one-shot buffer, which has finished by then: set first, play
   out, then mute.
2. **Write, read back, and rewrite until the chip agrees.** Writing once right
   after the first frame is still too early: the restore happens when the
   hardware buffer actually starts, not when you hand the frame to the FIFO
   (`iio_writedev` may not have consumed it). Asked for -20.00 dB, a reused
   transmitter can report -30.00, the PREVIOUS stream's value.
3. **Mute BEFORE you tear the buffer down, never after.** Closing first hands
   the cache your loud value for the next program: a bare buffer enable on a
   board reading −89.750000 comes up at **−61.500000**, 28.25 dB nobody asked
   for.
4. **Check both attenuators immediately after every buffer enable**, because the
   enable itself can raise one.

**Four tools here stream**, and all four follow these rules: the selftest (the
one CI runs), `sample_gpio_clock.py`, `modulation-gallery/board.py` and
`tx-gpio-bitmap-check.py`. Any new streaming tool must too. See
[`rf-safety.md`](references/rf-safety.md).

**Wait for a killed transmit writer to be gone before muting or starting
another.** When `iio_writedev` finally exits, the kernel's close hook mutes the
transmitter; if a NEW writer has meanwhile set its gain, the radio sits at
-89.75 dB with the gain read-back already passed, so every second transmitter
in a sweep comes up dead. `pgrep -x` matches the process NAME, so unlike
`pgrep -f` it cannot match the shell running it.

**Raising TX output needs an affirmation on record; antenna presence is not
detectable.** This board has no coupler and no detector on transmit, so nothing
can tell you what is on the port. `./devkit tx-guard affirm <0|1>` records what
a person says, per channel, and dies at the next reboot; `./devkit selftest
--loopback`, `tools/sample_gpio_clock.py` and `tools/modulation-gallery/board.py`
all refuse without it. `./devkit selftest` on its own is untouched. Muting is
never gated. The termination cases behind this are in
[`IDLE-CASES.md`](../../../IDLE-CASES.md).

**Vivado is not required to build.** `./scripts/build_all.sh --xsa FILE` takes
an already-built hardware platform and skips stage `[1/7]` entirely, so a
kernel/driver/rootfs change needs no Vivado at all. `verify_output.sh` then
describes the design from the platform's own `system.hwh` and reports timing as
unavailable rather than failing. The resulting `BOOT.bin` is byte-identical.
See [`docs/building-without-vivado.md`](../../../docs/building-without-vivado.md).

**MATLAB sees ONE of the two receivers, and its full scale depends on the
output type.** `ChannelMapping must be equal to 1` on *both* `sdrrx` and
`sdrtx`: the ADALM-Pluto support package is written for a 1R1T radio. RX2 and
TX2 are reachable only through `fishball.capture2` / `fishball.safeTransmit`,
which go via `iio_readdev` and `iio_writedev -c`. Full scale: `int16` gives raw
counts (**±2047**), `double`/`single` give counts÷**2048** (±1.0), transmit is
**±32767**; mixing the first two is a 66 dB mistake that raises no error. Setting a
property on a running System object does nothing; `release()` and rebuild.
**Never accept MATLAB's offer to update the firmware**: that image is for a
Zynq-7010 ADALM-Pluto. A `git describe` in `fw_version` stops MATLAB connecting
outright, which is why `fishball-identity` publishes `fw_version` and
`fw_build` separately. For Simulink, `fishball.RxSource` / `fishball.TxSink`
reach both channels and MUST run with `SimulateUsing = 'Interpreted execution'`:
they call `system()`, which cannot be code-generated, and the default setting
fails to compile with a message that names nothing. Details and measurements:
[`docs/matlab.md`](../../../docs/matlab.md).

**A Simulink System object must not touch the radio in `setupImpl`.** Simulink
calls it during COMPILE as well as at start, so a transmitter opened there is
started, torn down and started again, with the radio silent in between (the
model's own receive log reads 1 count of 2047). Open lazily on the first step;
keep only argument checking in setup. At the full 2.304 MSPS MATLAB cannot keep
up, so receive buffers stay full and samples arrive about **34 frames late**:
engage the FPGA /8 decimator and the host keeps up.

**A retune is not visible in the samples for ~35 frames unless you rebuild the
buffer.** Over USB at 2.304 MSPS with 4096-sample frames, a looped tone stays at
the OLD offset for 34 more frames after a 500 kHz retune while
`altvoltage0 frequency` already reads the new value: `iio_readdev`, the FIFO,
the socket and the board's DMA ring all hold old samples. **Reading the register
back proves nothing**; measure the samples. Destroy and rebuild the buffer after
any configuration change (pyadi-iio's `rx_destroy_buffer()`), and the change
lands on the next frame.

**Two AD9361 attributes this firmware refuses, both with `Invalid argument
(22)`.** `rf_port_select` on receive accepts only `A_BALANCED`, although
`rf_port_select_available` advertises twelve including `TX_MONITOR1/2`; it is
refused from an idle ENSM state as readily as from a running one, so nothing
reaches the TX monitor path. `filter_fir_en 1` is refused until coefficients are
loaded through `filter_fir_config`. **Check `iio_attr`'s exit status** (1 on
refusal, 0 on success) and never send its errors to `/dev/null`, or a rejected
setting looks exactly like an applied one. The quadrature, RF DC and baseband
DC tracking enables do apply.

**Simulate before you synthesise.** `./sim/run_sim.sh` checks the custom HDL
against a golden model in about a second; a Vivado build is 20 minutes with
`--hdl-only` and 70 from cold, and synthesis cannot tell you the logic computes
the wrong thing. `--mutate` proves the testbench can still fail.

**Verify before you flash.** `./scripts/verify_output.sh` checks the five files,
that the bitstream is compressed (an uncompressed one overflows the FSBL's OCM
and BOOT.bin fails to boot with no message), and that timing is met. It prints
the DSP count and which coefficients are in use, so you can see your change
landed.

**When the radio misbehaves, ask what else is writing to it. The answer
depends on the userspace.**

On `firmware/` (Buildroot) it is `/mnt/jffs2`: the one writable persistent
partition, whose `autorun.sh` runs at every boot. Scripts there survive
reflashing the kernel, device tree and bitstream, appear nowhere in the firmware
source, and can rewrite IIO attributes underneath an application; check it
before rebuilding any kernel over a "firmware bug". `sdr_selftest.py --ssh`
lists what is there.

On `firmware-modern/debian` **nothing runs `autorun.sh`** (no reference to it
from systemd, `/etc/init.d` or `rc.local`). `/mnt/jffs2` is still mounted
(`/dev/mtdblock2`) but its only job is `hw_serial`, minted once by
`fishball-identity.service`. The root is ext4 and writable, so it is not "the
one writable partition" either. What moves settings underneath you there is
systemd:

```bash
# run on the board (Debian)
systemctl list-units --failed
journalctl -b -u iiod -u fishball-identity -u fishball-rf-quiesce
```

So a persistent `autorun.sh` customisation *silently stops running* when a
board moves to Debian, and an `autorun.sh` left over from Buildroot is dead
weight that looks live.

**No libiio but ssh works, on Debian? iiod is held back by design.** It
`Requires=fishball-rf-quiesce`: if the boot mute could not be proven there is
no SDR service, and `fishball-usb-bind` binds the gadget WITHOUT iiod's USB
function so usb0 still comes up. Read the journal above; fix; reboot.
`systemctl start iiod` is safe: `Requires=` re-runs the quiesce first and iiod
starts only if it passes. Do NOT run `/usr/sbin/iiod` directly: that is the one
way around the check that the transmitter is quiet.

**Do not change the device tree without a strong reason.** On `firmware/` it
recompiles byte-for-byte identical to the factory board's, which is a
provenance claim; patch `0008` (`gpio-line-names`, so the sample-locked pins
resolve via `gpiofind sample_gpio0`) is the single exception, kept as its own
patch so dropping it restores the factory `.dtb`. On `firmware-modern/` there
is no byte-identity to protect, and the reason inverts: the tree is the one
place a setting cannot be changed without a reflash, so a trigger or a default
belongs in the driver or in the rootfs's own init. On `firmware-modern/` that is
`fishball-rf-quiesce.service`, **not** `S21misc`, which belongs to the
Buildroot userspace. Either way, most things people reach for the device tree
for belong elsewhere.

**Check a device tree by building it, not by reading it.** On
`firmware-modern/` the `.dts` is an overlay on ADI's `zynq-pluto-sdr.dtsi`, so
most of what lands in the `.dtb` is not in the file you edited. Two failures
that are invisible in the `.dts` and still boot: a `memory@0` node that becomes
a *sibling* of the dtsi's `memory` (the tree carries both 512 MB and 1 GB, and
`dtc` says only "duplicate unit-address" against an unrelated node), and ADI's
`&sdhci0 { status = "disabled" }`, which means a card-reader trip because
`tools/flash.sh` works by mounting `/dev/mmcblk0p1` on the running board. Run
`python3 firmware-modern/verify_dtb.py <built.dtb>`: 16 checks, including that
the transmit-attenuation default is still 89750 mdB and that nothing the
factory tree enables has gone missing. CI runs it too.

**A loopback without an attenuator destroys the receiver.** The RX input is
rated to about +2.5 dBm; plan for **about +19 dBm** flat out (an estimate, not
a meter reading). Fit at least 20 dB, and measure through exactly 20 dB: bigger
pads let the board's own TX->RX leak into the result. Details in `rf-safety.md`.

## Where things are

| | |
|---|---|
| `devkit` | the entry point: doctor, setup, sim, build, verify, flash, selftest, gpio-check, net, status, and more |
| `firmware/scripts/doctor.sh` | can this machine build? run before the hour, not during |
| `tools/flash.sh` | flash the running board over the network, safely (`./devkit flash`) |
| `tools/droneid/` | DJI DroneID: the board receives (unchanged firmware), the host decodes (`droneid_rx.py`) into `dji_receiver.py`. Receive only. Also why MicroPhase's ANTSDR E200 image cannot run here: its bitstream drives nine balls this board's AD9361 drives |
| `tools/make-sd-card.sh` | write a bootable FACTORY card from scratch: the recovery route when the board will not boot. Refuses anything not a removable USB/MMC whole disk |
| `firmware-modern/debian/write-card.sh` | write the two-partition DEBIAN card (vfat `/boot` + ext4 root). Refuses a `rootfs.tar` older than `overlay/` |
| `tools/net.sh` | DHCP or a static address, permanently; finds the board again afterwards (`./devkit net`) |
| `tools/board_addr.py` | where the board is: the one resolver every tool uses; never hard-code an address. `--check` prints it AND exits non-zero if it does not answer |
| `docs/networking.md` | where the address lives, the two names, and why the SD card's uEnv.txt is a decoy |
| `tools/tx-gpio-bitmap-check.py` | verify the sample-locked GPIO outputs on hardware (`./devkit gpio-check`) |
| `docs/tx-gpio-bitmap.md` | the sample-locked GPIO feature, end to end |
| `matlab/+fishball/` | MATLAB package: `connect`, `capture2`, `spectrum`, `phase`, `evm`, `safeTransmit`, `readSigMF`, `doctor` (`./devkit matlab`) |
| `examples/matlab/` | six MATLAB examples, receive-first; 03 transmits, 04 transmits when given `TxChannel`, 06 is Simulink and its QAM model transmits |
| `docs/matlab.md` | MATLAB end to end; read it before letting MATLAB near the firmware |
| `tools/clock-cal.py` | measure the 40 MHz reference against a disciplined source and set `xo_correction` (`./devkit clock`) |
| `firmware/patches/` | what makes this board's firmware; `setup.sh` applies these |
| `firmware-modern/` | the current kernel: `setup.sh`, `patches/` (9), `dts/`, `config/`, `verify_dtb.py`, `baseline/` |
| `firmware/patches/optional/` | worked examples, **not** applied by default (just the FM channelizer) |
| `firmware/src/` | upstream source, created by `setup.sh`, not committed |
| `firmware/output/` | the five SD-card files |
| `firmware/sim/` | Icarus Verilog testbenches for the custom HDL |
| `firmware/scripts/verify_output.sh` | pre-flash sanity check |
| `tools/selftest/` | is the board damaged? measures and says |
| `docs/block-design.md` | the stock Vivado project, IP by IP |
| `docs/measured-performance.md` | what one board actually does |

**The patches**, in order (full catalogue:
[`firmware/patches/README.md`](../../../firmware/patches/README.md)):

- `0001` fixes and a persistent serial; `0002` the device tree.
- `0004` mutes TX when no DMA stream; `0005` stops the unmute overwriting a gain
  set before the stream.
- `0006` routes each TX sample's low nibble (the bits the 12-bit DAC discards)
  to JP5 pins 7/9/11/13 (balls V10/U9/U10/T9, bank 13, 3.3 V, pulled down);
  `0007` adds the `tx_sample_gpio_en` sysfs attribute that enables it. Both edit
  files 0004/0005 also touch (`cf_axi_dds.c`), so a new patch there must be
  generated against a reconstructed pre-change file, never a plain `git diff`.
- `0008` names those GPIO lines in the device tree (on `firmware-modern/` the
  names are part of the tree). `0009` gives the bit-map flag's clock-crossing
  constraint the `-from` it lacked: `set_max_delay -datapath_only` needs both
  ends, and without one Vivado drops the line silently.
- `0011` probes the transmitter at maximum attenuation rather than 10 dB, which
  also makes a debugfs `initialize` land on silence.
- `0012` makes the USER LED follow the transmitter, so the board shows when it
  is keyed.
- `0013` pins eth0 to the MAC U-Boot already uses and sends a hostname in the
  DHCP request. Without it the macb driver logs "invalid hw address, using
  random" and picks a new MAC every boot, so a router sees a new device each
  time and a DHCP reservation is impossible. `0014` makes the default hostname
  `fishball` (0013 had set `Fishball7020`), so the board answers to
  `fishball.local` rather than `pluto.local`.
- `0015` mutes when the DAC stops being fed, because `postdisable` is an event
  and events get missed. It corrects a claim `0004` made; see
  [`rf-safety.md`](references/rf-safety.md).
- `0016` adds the `tx_disable` latch that debugfs cannot clear. `0017` counts TX
  DMA underflows; `0018` refuses to get louder above a die temperature.
- `0020` is a build fix with no radio behaviour: the host tools use U-Boot's own
  libfdt headers instead of the system's, which break stage [3/7] on any host
  that has `libfdt-dev` (or, on Arch, `dtc`).
- `0021` sends **both** receive channels through the ÷8 decimator. Upstream
  filters channel 0 and wires channel 1 straight to `cpack`, which samples it on
  channel 0's valid with no anti-alias filter of its own: about 70 dB of
  aliasing on RX2 the moment decimation engages. It is applied **by default**
  (formerly `optional/0004`); `STOCK_RX_FILTER=1` builds upstream's wiring
  instead. It costs 22 DSP48s and ~625 LUTs, and the optional channelizer sets
  `rx_filt_chan 2` for its own worked example.
- `optional/0003` is the FM channelizer.
- **`0019`, `firmware-modern/` only: read it first.** `ad9361_clear_state()`
  memsets the struct that held the attenuation the kernel restores when it
  unmutes, and 0 mdB is **full output**, so a debugfs `initialize` followed by
  any transmit stream keys the transmitter flat out. That is the third
  safety-relevant field moved out of `ad9361_rf_phy_state`, after `0016`'s latch
  and `0018`'s limit, so **never add a safety field there**. The same code is
  still on `firmware/`.

To fix an applied patch, add a new one on top. Never edit it: `setup.sh` cannot
re-apply a patch over its earlier version, so an edit breaks every existing
tree.

## Typical work

**An HDL change**: edit, `./sim/run_sim.sh`, delete the Vivado project,
`./scripts/build_all.sh --hdl-only`, `./scripts/verify_output.sh`, flash
`BOOT.bin`, then `sdr_selftest.py --ssh`.

**A kernel change**: edit `src/linux/`, rebuild `uImage` alone (a few minutes;
the full `build_all.sh` is not needed), flash `uImage`, reboot. Then fold the
change into a numbered patch so a fresh clone gets it, and add a CI assertion:
`firmware-modern/patches/` with `verify-modern.yml`, or `firmware/patches/` with
`verify-patches.yml`. On `firmware-modern/` the loop is shorter (no `PATH`
juggling, and the defconfig names everything); either ARM Linux GCC works as
`CROSS`:

```bash
# run from: firmware-modern/src/linux
CROSS=../../../firmware/src/buildroot/output/host/bin/arm-linux-gnueabihf-
make ARCH=arm CROSS_COMPILE=$CROSS uImage LOADADDR=0x8000 -j$(nproc)   # ~2 min
cp arch/arm/boot/uImage ../../output/
# run from: the repo root
./devkit flash --target modern --kernel-only
```

**Diagnosing the radio**: `sdr_selftest.py --ssh` first. It is read-only, never
transmits, and reports supply rails, die temperatures, the AD9361 interface eye,
the internal digital loopback and the receiver. Add `--loopback --pad <dB>`
only with a cable and attenuator fitted.

**Before a release**: run a clean-clone end-to-end build. It exercises the
build's own self-repair path, whose defects stop only the next person building
from a fresh clone.

## What a healthy board looks like

From one unit, so indicative rather than specification. Use it to judge
whether something is actually wrong.

| | |
|---|---|
| Gain slopes (TX attenuator, RX gain) | within **1.7% of 1.000 dB/dB** (56 slopes) |
| Image rejection, after a fresh TX quad calibration | **44–60 dBc** (31–54 as found), 5–7 dB worse into RX2, varies up to 10 dB run to run |
| Harmonics | 2nd **−64 to −80 dBc**, 3rd **−71 to −85 dBc** |
| Transmit power flat out | about **+19 dBm**: the self-test's capped estimate, never metered |
| TX mute depth | **at least 75 dB** (every reading hit the noise floor) |
| Loop gain, 200 MHz – 1 GHz | ~**+20 dB** (flat to 2 dB), pad added back |
| Board's own TX->RX leak, as an equivalent pad | channel 0: 58–77 dB below 1 GHz, **33–51 dB** at 3–6 GHz; channel 1 ~10 dB weaker; crossed paths 10–35 dB weaker still |
| Supply rails | all six within **1.6%** of nominal |
| Digital interface eye | **157–158** of 256 delay positions pass (27 of 28 recorded runs read exactly 157) |
| On 6.12, TX0 looped to RX0 through 20 dB | `./devkit selftest --loopback --pad 20`: **32 passed, 0 failed**. TX attenuator 1.007 dB/dB, image rejection below the capture floor, mute depth 73.1 dB to the floor, loop gain −0.1 dB through the declared pad |
| FPGA, default build | 94/220 DSP48s, 12 521 LUTs, WNS **+0.215 ns** over 54 211 endpoints (with 0009 and 0021, both RX channels filtered). `STOCK_RX_FILTER=1` gives upstream's wiring: 72/220, 11 896 LUTs, +0.205 ns over 48 263. Builds vary by a few hundredths; the worst path is in ADI's DMA |

The two channels on one board differ by 1.5 dB in receive and 0.1–0.25 dB in
transmit, so some asymmetry is normal. Full data:
`docs/img/data/measured-performance.json`.
