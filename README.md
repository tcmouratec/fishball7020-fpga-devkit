<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/covers/devkit-stack-dark.png">
    <img src="docs/img/covers/devkit-stack-light.png"
         alt="Every layer, from source — one command rebuilds all five and flashes them back over the network. The five layers listed: bitstream (Vivado to system_top.bit), FSBL (AMD embeddedsw with gcc-arm-none-eabi), U-Boot (distro cross-compiler), kernel (Linux 6.12 LTS with ADI drivers) and root filesystem (Debian 13 armhf). PlutoSky R1 / 7020-SDR, Zynq XC7Z020 with an AD9361.">
  </picture>
</p>

# Fishball7020 FPGA Devkit

### Editable firmware for a two-channel SDR that ships without any.

The **PlutoSky R1** — also sold as **7020-SDR**, **Fishball7020**,
**PlutoSky_7020_AD936X_SDR** and **Fish-Wan** — is a Zynq XC7Z020 + AD9361
radio, 70 MHz to 6 GHz, two transmit and two receive channels. It arrives with no
published, buildable source. This reconstructs it: you open the real block
design, put your own HDL next to the AD9361 datapath, rebuild every layer
(bitstream → FSBL → U-Boot → kernel → rootfs) and flash it back over the
network without opening the case.

> **Not the official Analog Devices / OpenSourceSDRLab repository.** This is an
> independent, reverse-engineered reconstruction, verified as close to
> bit-perfect as public sources allow — [how that is
> verified](docs/provenance.md).

<p align="center">
  <a href="https://matsvandamme.github.io/fishball7020-fpga-devkit/course/"><img src="https://img.shields.io/badge/course-Fabric%20School%20%C2%B7%2053%20lessons-8A3FFC" alt="Fabric School: a 53-lesson SDR and FPGA course for this board"></a>
  <img src="https://img.shields.io/badge/board-Zynq%20XC7Z020%20%2B%20AD9361-blue" alt="Board: Zynq XC7Z020 + AD9361">
  <img src="https://img.shields.io/badge/toolchain-Vivado%202022.2-orange" alt="Toolchain: Vivado 2022.2 (no Vitis)">
  <img src="https://img.shields.io/badge/host%20OS-Ubuntu%2022.04%20LTS-e95420" alt="Host OS: Ubuntu 22.04 LTS">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-GPL--2.0-lightgrey" alt="License: GPL-2.0"></a>
  <a href="../../actions/workflows/verify-patches.yml"><img src="https://github.com/matsvandamme/fishball7020-fpga-devkit/actions/workflows/verify-patches.yml/badge.svg" alt="Verify patches CI status"></a>
  <a href="../../actions/workflows/verify-modern.yml"><img src="https://github.com/matsvandamme/fishball7020-fpga-devkit/actions/workflows/verify-modern.yml/badge.svg" alt="Verify the modern firmware CI status"></a>
</p>

<p align="center"><img src="docs/img/board.jpg" alt="Fishball7020 / PlutoSky SDR board — Zynq XC7Z020 with AD9361, 4x SMA connectors, Ethernet and USB" width="480"></p>

New to any of this? **[How it works](docs/how-it-works.md)** starts from the
beginning and assumes nothing — or take
**[Fabric School](https://matsvandamme.github.io/fishball7020-fpga-devkit/course/)**,
the 53-lesson course written against this exact board.

---

## Is this your board?

This targets one board: the one sold as
[**"7020-SDR" (XC7Z020 + AD9361, dual TX/RX)**](https://nl.aliexpress.com/item/1005012055627197.html),
also listed as PlutoSky, PlutoSky R1, PlutoSky_7020_AD936X_SDR and Fish-Wan.
Listings drift; the board's own report does not:

```bash
# run on your HOST, from anywhere (needs libiio-utils)
iio_attr -S
#  1: 192.168.2.1 (FISH Ball PlutoSDR Rev.A (Z7020-AD9361)), serial=... [ip:fishball.local]
```

`FISH Ball PlutoSDR Rev.A (Z7020-AD9361)` fits. `Z7010`, `AD9363` or another
Rev does not — the original ADALM-PLUTO and other AD936x boards use different
pins and will not work with this firmware unchanged.

**The PlutoSky R1 ships in more than one configuration**, and only one of them is
this board. The distributor's own manual lists the SoC as `XC7Z020-2CLG400I` —
which is the part this project targets — with *"ADI's AD9361 or AD9363"*, and
sells both *"PlutoSky R1 with PA"* and *"PlutoSky R1 without PA"*. This firmware
is built and measured against the **AD9361** variant, and the RF safety figures
throughout assume the **power amplifier is fitted**. An AD9363 unit will not work
unchanged; a unit without the PA is safe to run this firmware on, but every
transmit power figure here will overstate what leaves the connector.

## What you get

- **Firmware that matches a factory unit**, rebuilt from source with one
  command — device tree byte-for-byte identical, rootfs and bootloader
  matching by content. ([How it was verified](docs/provenance.md))
- **ADI's real block design**, open in Vivado, so your HDL sits *in* the
  AD9361 datapath rather than beside it. ([IP by IP](docs/block-design.md))
- **A transmitter that mutes itself within 250 ms if the program feeding it dies**,
  which stock firmware does not — and that comes up at maximum attenuation rather
  than at the 10 dB the factory device tree asks for. It is *not* "off unless you are
  transmitting": opening a DMA buffer restores a cached gain on its own, and every
  power-on emits a few milliseconds from the chip's own calibration before any
  software exists. Both are measured in [`IDLE-CASES.md`](IDLE-CASES.md).
  ([Transmitter safety](docs/transmitter-safety.md))
- **A USER LED that means something**: lit whenever RF can leave either port.
  ([USER LED](docs/user-led.md))
- **Two receivers that both survive decimation — by default.** Stock ADI wiring
  filters only channel 0, leaving channel 1 aliased by 70 dB the moment the
  fabric decimator engages. Patch `0021` puts both through it for 22 DSP slices;
  `STOCK_RX_FILTER=1` builds upstream's wiring if you want it back.
  ([Both channels](docs/both-receive-channels.md))
- **Four header pins that tick with the transmitted waveform**, carrying the
  bits the DAC throws away. ([Sample-locked GPIO](#sample-locked-gpio-outputs))
- **A self-test that answers "is this board damaged?"** with measurements, not
  with "it still enumerates". ([Is the board healthy?](#is-the-board-healthy))
- **You do not need Vivado** to change the kernel, a driver or the rootfs —
  build from a released hardware platform instead and skip the 50 GB install.
  ([Building without Vivado](docs/building-without-vivado.md))
- **A current kernel, not a fork of a fork.** Linux **6.12 LTS** from Analog
  Devices, in place of the vendor's 5.15 — with the same measured transmitter
  safety and the same RF numbers. ([Why this kernel](docs/modern-kernel.md#why-adi-612-and-not-mainline))
- **An agent skill** in [`.claude/skills/`](.claude/skills/fishball7020-firmware/SKILL.md)
  carrying the rules that were expensive to work out. Ignore it if you do not
  use an agent.

> ### 📡 [What it puts on the air](docs/modulation-gallery.md) — measured, not simulated
>
> Ten modulations transmitted by one Fishball7020 and received on a **HackRF
> One**: CW, OOK, 2-FSK, BPSK, QPSK, GMSK, 16-QAM, 64-QAM, OFDM and a
> LoRa-style chirp. Spectra with ~85 dB of clean dynamic range, constellations
> recovered over the air, and a spur traced back to whichever radio made it.
>
> [![Ten modulations transmitted by a Fishball7020 and received on a HackRF One: ten spectrum panels showing CW, OOK, 2-FSK, BPSK, QPSK, GMSK, 16-QAM, 64-QAM, OFDM and a LoRa-style chirp, each about 85 dB above the muted noise floor.](docs/img/modulation/01-signal-set.png)](docs/modulation-gallery.md)
>
> The receiver is a *separate* radio on purpose: a board receiving its own
> transmission shares one clock with itself and hides every oscillator problem.

## Two firmware targets, and which to use

| | [`firmware-modern/`](firmware-modern/README.md) | [`firmware/`](firmware/README.md) |
|---|---|---|
| kernel | **6.12 LTS**, Analog Devices' `main` | 5.15, the vendor's fork |
| userspace | **Debian 13 armhf + systemd**, on the SD card | Buildroot and busybox, in RAM |
| FPGA | taken from an XSA (the FPGA design in one file) | built with Vivado, or taken from an XSA |
| answers | *"what should this board run?"* | *"what does a factory board run?"* |

**Use `firmware-modern/`** unless you specifically want the factory kernel. It
builds in minutes with no Vivado, and gives you `apt`, a writable root and
persistent logs.

**`firmware/` is where the FPGA design lives**: the only bitstream source in the
repository, and the byte-for-byte factory reconstruction that the
[provenance](docs/provenance.md) claim rests on. Change the FPGA there; the
modern target takes the resulting XSA.

`./devkit` drives both. The factory target is the default; **add
`--target modern`** to `setup`, `build`, `verify`, `flash`, `doctor`, `status`
and `write-card`.

**Why 6.12 and not mainline Linux?** Mainline has no AD9361 driver, and cyclic
transmit depends on an Analog Devices change to the IIO core that only ADI's
tree carries. The modernisation was [MrMati](https://github.com/MrMati)'s
proposal ([issue #4](../../issues/4)).
[`docs/modern-kernel.md`](docs/modern-kernel.md#why-adi-612-and-not-mainline)
has the reasoning.

## Quick start

### Just want a working board?

1. **Back up your card first.** Copy every file off the board's microSD card.
   That is your way back.
2. Download a release and put it on the card:
   - **[v1.7](../../releases/tag/v1.7)**, the factory firmware: copy its five
     SD-card files onto the FAT32 card. Nothing else to install.
   - **[the latest release](../../releases/latest)**, the modern firmware:
     it needs two partitions, so write it with a clone of this repository and
     a card reader:
     `sudo ./devkit write-card --target modern --from ~/Downloads /dev/sdX`
     (the release notes have the details).
3. Check the **`BOOT`** DIP switch next to `RST` is in SD mode: both sliders
   pushed away from `ON` (`0 0`). Boards ship like that.
4. Insert and power on. After about 40 s the board appears over USB at
   `192.168.2.1`.

Nothing happening? [Boot modes](docs/flashing.md#boot-modes-boot-dip-switch) ·
[recovering the factory firmware](docs/flashing.md#if-things-go-wrong-recovering-the-factory-firmware).

### Want to change the firmware?

The kernel, drivers or the Debian userspace, **no Vivado needed**:

```bash
# run from: wherever you want the devkit to live (e.g. ~)
git clone https://github.com/matsvandamme/fishball7020-fpga-devkit.git
cd fishball7020-fpga-devkit

./devkit doctor --target modern                  # can this machine build?
./devkit setup --target modern                   # fetch the sources, apply the patches (~0.6 GB)
XSA="$(./firmware-modern/fetch-pinned-xsa.sh)"   # the FPGA design of a factory release
./devkit build --target modern --all --xsa "$XSA"   # boot files, kernel and Debian root
sudo ./devkit write-card --target modern /dev/sdX   # the first time: a whole new card
```

After that, a changed kernel goes onto the running board over the network with
`./devkit flash --target modern --kernel-only`. No ARM cross-compiler on this
machine? Build the boot files in a container instead:
[`firmware-modern/`](firmware-modern/README.md#quick-start) shows how.

The FPGA itself, with Vivado:

```bash
# run from: the repo root
./devkit doctor          # can this machine build? finds out now, not at minute 40
./devkit setup           # clone upstream source + apply patches            (~5 min)
./devkit build           # everything                                    (45-90 min)
./devkit verify          # is the build sane?
./devkit flash --all     # onto the running board over the network, then reboot
./devkit verify --board  # is the board actually running it?
```

`./devkit --help` describes every subcommand and flag, grouped by what you are
trying to do, and it completes with tab:

```bash
# run from: the repo root
source <(./devkit completion)      # this shell
./devkit completion install        # every shell, from now on
```

**Three routes to a toolchain**, in the order most people should try them:

| | |
|---|---|
| **Without running Vivado** | `./devkit build --xsa FILE` skips the FPGA stage, given a hardware platform (`.xsa`). **[v1.6](https://github.com/matsvandamme/fishball7020-fpga-devkit/releases/tag/v1.6) onward ships one** (use the [newest](https://github.com/matsvandamme/fishball7020-fpga-devkit/releases/latest)) — factory releases only, since the modern target runs no Vivado. **And nothing else from AMD either** — the FSBL builds from AMD's embeddedsw with a plain cross-compiler, and `bootgen` is built from AMD's Apache-2.0 source. No Vivado, no Vitis, not even installed. ([how](docs/building-without-vivado.md)) |
| **A container** — recommended if you need Vivado | Vivado 2022.2 supports Ubuntu 18.04/20.04/22.04 and nothing newer. `./devkit container` sidesteps that, and installs Vivado for you. Verified byte-for-byte identical `BOOT.bin` to a host build. ([how](docs/building-in-a-container.md)) |
| **On the host** | Fine on Ubuntu 18.04/20.04/22.04. ([install](docs/building.md#install-vivado-20222)) |

Anything that touches the radio — `flash`, `selftest`, `gpio-check`, `temps` —
always runs on the host, container or not.

> ### Before you ever transmit
>
> The receive port survives **+2.5 dBm** — the AD9361 data sheet's own
> absolute-maximum rating. This board is sold in a variant with a power
> amplifier that reaches about **+19 dBm**, some 16 dB more than its own
> receiver tolerates. So **never loop TX back to RX without at least 20 dB of
> attenuation**, and never transmit at power into an open port. Most of this
> board's range is licensed spectrum.
> More in [Transmitter safety](docs/transmitter-safety.md).

## Your first hour

**Connect.** Use the **USB 2.0** socket, not `DEBUG`. It appears as a network
interface:

```bash
# run on your HOST, from anywhere
ssh root@192.168.2.1        # password: analog
ssh root@fishball.local     # over Ethernet it answers to its name instead
```

**Stop typing that password.** One command sets up a key used only for this
board, installs it, and adds an `ssh fishball` shorthand:

```bash
# run from: the repo root
./devkit ssh-key
ssh fishball
```

It proves the result with `BatchMode`, which cannot fall back to a password, so
a pass means the key really is doing the work.
([details](docs/networking.md#logging-in-without-a-password))

`DEBUG` is the serial console and JTAG — only needed when the board will not
boot. ([which port is which](docs/flashing.md#verify-your-build-is-actually-running))

**Look at it.** None of these transmit, and nothing needs to be plugged into
the RF ports:

```bash
# run from: the repo root
./devkit status            # what is built, what the board is running
./devkit selftest --ssh    # is the radio damaged? answers with measurements
./devkit temps             # both die temperatures, live, against their ratings
./devkit net               # what address did it get, and how?
```

**You never type an address.** Every tool resolves `fishball.local` first and
falls back to the USB gadget at `192.168.2.1`; `tools/board_addr.py` is the one
place that order is decided, and `BOARD=` or `SDR_URI=` overrides it. To
software the board is a Pluto at `ip:fishball.local`, so libiio, pyadi-iio, MATLAB, GNU
Radio and SDRangel work with it as they would with a Pluto.

**On your network.** Ethernet takes a DHCP address and the board announces
itself as `fishball.local`. `./devkit net dhcp` and `./devkit net static <ip>`
switch modes permanently. This firmware also fixes two things stock gets wrong:
no hostname in the DHCP request, and a MAC regenerated at every boot — which
makes a DHCP reservation impossible.
([the four routes, and the SD-card file that looks like it works](docs/networking.md))

## Where to go next

| I want to… | Start here |
|---|---|
| **use this board in a project of my own** | **[Using this board in your own project](docs/your-own-project.md)** — the four places your code can live, what each costs, and how to choose |
| learn this from nothing — SDR, Verilog and Vivado | **[Fabric School](https://matsvandamme.github.io/fishball7020-fpga-devkit/course/)** — 54 lessons written against this board ([190-page PDF](https://matsvandamme.github.io/fishball7020-fpga-devkit/course/Fabric-School.pdf)) |
| understand what the build produces and why | [How it works](docs/how-it-works.md) |
| install a toolchain, or avoid needing one | [Building](docs/building.md) · [in a container](docs/building-in-a-container.md) · [without Vivado](docs/building-without-vivado.md) |
| add my own HDL to the radio's datapath | [Add your own HDL](docs/building.md#add-your-own-hdl) · [the block design](docs/block-design.md) |
| **see the board do something, with controls that teach** | **[Examples](examples/)** — three graded GNU Radio showcases: [dynamic range and how to lose it](examples/01-dynamic-range/), [a QPSK link you can watch](examples/02-modulated-link/) (transmits), [two coherent receivers](examples/03-coherent-receivers/) |
| see a complete worked example | [An FM channelizer in the FPGA](docs/wbfm-channelizer.md) |
| check my HDL in a second, before a 20-minute build | [Simulating your HDL first](docs/building.md#simulating-your-hdl-first) |
| use both receivers with the FPGA decimator on | [Two receivers that survive decimation](docs/both-receive-channels.md) |
| **use this board from MATLAB or Simulink** | **[MATLAB](docs/matlab.md)** — and read it before you let MATLAB near your firmware · [six examples](examples/matlab/) |
| change a driver or the kernel | [Changing the kernel](docs/kernel.md) |
| **build the current kernel instead of the factory one** | **[firmware-modern](firmware-modern/README.md)** — Linux 6.12 LTS, why it and not mainline, and [the nine patches](firmware-modern/patches/README.md) |
| get off Buildroot and busybox, onto Debian | [Getting off Buildroot](docs/debian-rootfs.md) — what the boot path actually allows, and the one risk worth planning around |
| get my build onto the board | [Flashing the board](docs/flashing.md) · [JTAG, the fastest HDL loop](docs/flashing.md#option-d--jtag-temporary-but-the-fastest-hdl-loop) |
| capture IQ that is still useful in a year | [Capturing IQ](docs/capturing-iq.md) — SigMF sidecars, and a dropped-sample check |
| see what this board actually transmits | **[The modulation gallery](docs/modulation-gallery.md)** — ten modulations on a HackRF One, with the code to repeat it |
| see which Wi-Fi channels are busy around me | [Scanning the Wi-Fi bands](tools/wifi-scan/README.md) — a GNU Radio sweep of 2.4 and 5 GHz |
| drive the GPIO pins, from host, board or fabric | [GPIO](docs/gpio.md) — three routes, and which pins are free |
| blink the USER LED | [Controlling the USER LED](docs/user-led.md) |
| **detect DJI drones (DroneID) and feed DragonSync/TAK** | **[The DroneID receiver](tools/droneid/README.md)**: decoded on the host, into `dji_receiver.py` unchanged |
| run another Zynq board's SD image (e.g. MicroPhase's DJI DroneID) on this one | [Why the ANTSDR E200 DroneID image is silent here, and cannot work](docs/droneid-microphase.md) |
| put the board on my router, or fix its IP | `./devkit net dhcp` · [Changing the IP address](docs/networking.md) |
| drive the radio from an AI assistant | the sibling **[Fishball7020-mcp](https://github.com/matsvandamme/Fishball7020-mcp)** — 21 MCP tools |
| use a tool better suited than GNU Radio | [Other tools, and when they beat GNU Radio](docs/other-sdr-tools.md) — Maia SDR on the fabric, inspectrum, URH, pyadi-iio |
| fix a build that fails | [Troubleshooting](docs/troubleshooting.md) |

## What is on the board

<img src="docs/img/board-map.png" alt="The board photographed from above, with 22 labels: the four SMA ports, EXT_CLK, TX_LO and RX_LO, the AD9361, the Zynq XC7Z020, two MT41K256M16 DDR3L chips, the RTL8211F Ethernet PHY, the HR911130A RJ45 jack, the JP5 header, the BOOT DIP switch, the reset button, the microSD card and both USB-C sockets. Parts inferred from package and position rather than a legible marking have dashed rings and say likely: the four RF baluns, the two PGA-102+ amplifiers, the 40 MHz VCTCXO, the USB3320C, the FT2232H, the W25Q128 flash and the FAN1 header." width="860">

An **AD9361** transceiver (70 MHz – 6 GHz, two channels), a **Zynq XC7Z020**
(two ARM cores plus FPGA fabric), 1 GB of DDR3L, a power amplifier on each
transmit port, a balun per SMA port turning the chip's differential RF pins
into single-ended coax, gigabit Ethernet and USB. Every chip with its
datasheet, plus clocks, connectors and supply rails, read off the vendor
schematic: **[What is on the board](docs/hardware.md)**.

## Sample-locked GPIO outputs

The AD9361's DAC is 12 bits wide and discards the bottom four bits of every
16-bit sample. This firmware routes those four bits to pins 7, 9, 11 and 13 of
the `JP5` header instead, so **every pin edge belongs to one specific
transmitted sample**, at a fixed offset from its RF — usable as a clock, frame
marker or trigger for hardware that must stay in step with the transmitter.
It costs nothing: the DAC never sees those bits.

The idea of routing those least significant bits straight to the GPIO outputs
was suggested by **Akil0515** ([Telegram](https://t.me/Akil0515)) — see
[CONTRIBUTORS.md](CONTRIBUTORS.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/saleae-timing-dark.svg">
  <img src="docs/img/saleae-timing-light.svg" alt="Logic-analyser capture of the four sample-locked GPIO pins carrying a 4-bit counter at 5 MSPS, with the decoded value D, E, F, 0, 1 and so on under each 200 ns sample" width="760">
</picture>

Off by default. To try it with the transmitter silent:

```bash
# run from: the repo root
python3 tools/sample_gpio_clock.py --help    # needs: pip install pyadi-iio numpy
./devkit gpio-check                          # check the pins work, no scope needed
```

**The full story** — how the nibble reaches the pin, the pinout, a complete
Python example, measured timing and limits:
**[docs/tx-gpio-bitmap.md](docs/tx-gpio-bitmap.md)**.

## Is the board healthy?

If you have overdriven an input, transmitted into an open port, or the board
has simply stopped behaving, the self-test answers with measurements. Most
checks need nothing plugged in:

```bash
# run from: the repo root
./devkit selftest --ssh                          # no cable, never transmits
./devkit selftest --ssh --loopback --pad 20      # + the RF tests
```

It cannot overdrive your receiver even if you forget the attenuator: it never
transmits with less than 35 dB of its own attenuation.
([what it checks](tools/selftest/README.md))

## Measured performance

One board, 28 runs. Gain settings do what they say to within 1.7%, harmonics
sit at least 63 dB below the carrier, the two transmitters match to 0.2 dB, and
the transmitter goes at least 75 dB quiet when it stops.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/loop-gain-dark.svg">
  <img alt="TX to RX loop gain for both channels from 70 MHz to 6 GHz, through the same 20 dB attenuator. Both rise from 12-14 dB at 70 MHz to a plateau near +20 dB between 200 MHz and 1 GHz, then fall to about +2 dB at 6 GHz. Channel 1 runs about 1.5 dB hotter. A step up at 4 GHz is marked as the AD9361 changing RX gain table. The region above 3 GHz is shaded where the board's own TX-to-RX leak can add up to 2 dB." src="docs/img/loop-gain-light.svg">
</picture>

Two traps before measuring your own: a gain calibration made below 4 GHz is
wrong above it, because the AD9361 changes receive gain table there; and the
board leaks its own transmit signal into its receiver, which spoils loopback
measurements through large attenuators — use 20 dB.

Full tables and how the numbers were checked:
**[measured performance](docs/measured-performance.md)**. With real modulated
signals, and where the streaming ceiling comes from:
**[modulation and throughput](docs/modulation-and-throughput.md)**.

## How fast can you actually stream?

The radio runs at up to **61.44 MS/s**, but a stream to your PC is limited by
the link, not the board. Each complex sample is 4 bytes, so one channel at the
full rate is 246 MB/s, about twice what gigabit Ethernet carries in each
direction. On the board itself, capture sustains 49.8 MS/s on one channel and
46.2 MS/s on each of two.

- **Raise the libiio buffer** (`-b 1048576` or larger). A small buffer costs
  about two thirds of the rate.
- **The USB link is far slower than Ethernet**, around 1.7 MS/s.
- **For the full rate, keep the host out of the loop**: a cyclic transmit
  repeats one buffer in hardware, and filtering or decimating in the FPGA
  sends fewer bytes. A cyclic transmit keeps going if the program that started
  it dies. The modern firmware stops it after 60 s by default; the factory
  firmware does not ([transmitter safety](docs/transmitter-safety.md)).

```bash
# run on your HOST
iio_readdev -u ip:fishball.local -b 1048576 -s 33554432 cf-ad9361-lpc voltage0 voltage1 > capture.iq
```

Every RX/TX combination, the buffer sweep and how the numbers were taken:
**[modulation and throughput](docs/modulation-and-throughput.md)**.

## Repository layout

```
fishball7020-fpga-devkit/
├── devkit              ← the one entry point: doctor, setup, build, flash, ...
├── docs/               ← everything this page links to
├── firmware/           ← THE FPGA LIVES HERE, plus the factory kernel (5.15)
│   ├── src/hdl/        the Vivado design — the ONLY bitstream in the repo
│   ├── scripts/        the build: HDL → bitstream → FSBL → U-Boot → BOOT.bin
│   ├── sim/            one-second HDL simulation, no Vivado
│   ├── patches/        the kernel, U-Boot and HDL patches; applied by setup
│   └── output/         the five SD-card files a build produces
├── firmware-modern/    ← the KERNEL AND USERSPACE, replaced (6.12 + Debian)
│   ├── setup.sh        fetch ADI's kernel at a pinned commit, patch it
│   ├── patches/        nine driver patches: eight rebased, one the rebase found
│   ├── dts/            the board's device tree, as an overlay
│   ├── config/         the kernel configuration, and why each option is there
│   ├── debian/         the rootfs: packages.txt, Containerfile, units, card writer
│   ├── verify_dtb.py   audit a built device tree against what the board needs
│   ├── baseline/       what the board reported, per kernel, for diffing
│   └── output/         BOOT.bin, uImage, devicetree.dtb, uEnv.txt
└── tools/              flashing, the self-test, the GPIO and RF tools
```

`firmware-modern/` never runs Vivado: its `BOOT.bin` is built from an XSA you
give it, and it replaces the kernel and the userspace.
[Which to use](#two-firmware-targets-and-which-to-use).

File by file: [Building your own firmware](docs/building.md#repository-layout).

## Getting help

Build failed, or the board acting up? [Troubleshooting](docs/troubleshooting.md)
and the [self-test](#is-the-board-healthy). Still stuck?
[Open an issue](../../issues/new/choose) — the templates ask for the details
that speed things up. Contributions welcome: [CONTRIBUTING.md](CONTRIBUTING.md)
· [CONTRIBUTORS.md](CONTRIBUTORS.md).

## Credits

- **[MrMati](https://github.com/MrMati)** proposed modernising the Linux side
  ([issue #4](../../issues/4)) and made the case that got it done. His
  [`luckfox-linux`](https://github.com/MrMati/luckfox-linux) is the same problem
  solved once already on another vendor-locked board — Debian trixie armhf,
  systemd, a current kernel — and its reasoning about vendor trees versus
  mainline is what made ADI's 6.12 the obvious place to start here.
- **Akil0515** suggested the sample-locked GPIO outputs.

Full list, including people who changed what this firmware does without pushing
a commit: [CONTRIBUTORS.md](CONTRIBUTORS.md).

## Vendor resources

- [**Hardware schematic**](docs/vendor/7020_936x_SDR-schematic.pdf), kept here
  because the vendor's own GitHub copy is a **different revision** that does
  not describe this board. [Which is which](docs/vendor/README.md).
- [PlutoSky R1 write-up](https://blog.opensourcesdrlab.com/archives/PlutoSky-R1) ·
  [vendor file archive](https://workupload.com/archive/kc2v7ryVZZ) ·
  [factory binaries](https://github.com/OpenSourceSDRLab/PlutoSky_7020_AD936X_SDR)

None of it includes editable HDL sources, which is the gap this repo fills.

## License

This repo's own scripts, patches and documentation are **GPL-2.0**. The
upstream source that `setup` downloads (Linux, U-Boot, Buildroot) stays GPL;
AMD's embeddedsw is MIT and its bootgen is Apache-2.0; Xilinx Vivado and AMD IP
are proprietary and licensed separately.
The breakdown is in [`LICENSE`](LICENSE).

One directory is not ours:
[`.claude/skills/goal-creator/`](.claude/skills/goal-creator/) is a third-party
agent skill vendored under **MIT**, with its own `LICENSE` and a
[`VENDORED.md`](.claude/skills/goal-creator/VENDORED.md) recording where it came
from and at which commit. Nothing in the firmware or the build depends on it.
