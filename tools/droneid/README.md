# tools/droneid: checking another board's Zynq image against this one

Two scripts, written to find out why MicroPhase's ANTSDR E200 DroneID image is
silent on a PlutoSky. The findings are in
[`docs/droneid-microphase.md`](../../docs/droneid-microphase.md). Both scripts
are general: they answer "will this other Zynq-7020 board's image work here?"
for any board.

Both need only Python 3: no bootgen, no Vivado.

## `zynq_bootimg.py`

```bash
# run from: the repo root
tools/droneid/zynq_bootimg.py info    BOOT.bin                    # partitions, md5s, compressed or not, IDCODE
tools/droneid/zynq_bootimg.py split   BOOT.bin OUTDIR             # write each partition out
tools/droneid/zynq_bootimg.py ps7     BOOT.bin                    # what the FSBL's ps7_init sets: PLLs, FCLKs, MIO, DDR, UART
tools/droneid/zynq_bootimg.py ps7diff A/BOOT.bin B/BOOT.bin       # where two FSBLs disagree, and which UART each uses
tools/droneid/zynq_bootimg.py graft   OUT.bin --fsbl A --bit B --uboot A
```

`ps7` and `ps7diff` read the register-write tables that `ps7_init.c` compiles
into an FSBL. An FSBL carries one set per silicon revision and the compiler
picks their order, so the silicon-3.0 set is told apart by its DDR table (81, 83
and 82 operations for revisions 1.0, 2.0 and 3.0). The decode was checked
against the factory XSA's own `ps7_init.c`.

`graft` keeps everything before and including the `--fsbl` image's FSBL byte
for byte and rewrites only the partition headers. Grafting an image onto itself
reproduces it exactly, for both the factory and the MicroPhase `BOOT.bin`, and
`firmware/scripts/check_bootbin.py` reads a graft back as the three expected
partitions. **Flash a graft only if its bitstream was built for this board**
(see below).

## `pinmap_compare.py`

```bash
# run from: the repo root
tools/droneid/pinmap_compare.py THIS_BOARD.xdc OTHER_BOARD.xdc
```

Compares the two constraint files ball by ball and marks each ball where the
other board's bitstream would drive a trace that something on this board
already drives. It exits 1 if there is any such contention. A bitstream fixes
the direction of every pin, so DONE lighting up proves only that the bitstream
loaded, not that it belongs on this board.
