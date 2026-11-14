"""Python wrapper around Icarus Verilog for the sim oracle.

Given a DUT module name and a latency-cycles parameter, compile the
DUT + ``tb_crossing.sv`` + ``meta_flop_lib.sv`` with ``iverilog``,
run with ``vvp``, parse the single ``SIM_RESULT errors=N total=M``
line emitted at end-of-run, and return a :class:`SimResult`.

The compilation is cached by SHA-256 of the SV inputs + macros, so
repeated runs of the same DUT with the same macros are an in-memory
load + a fresh ``vvp`` invocation. The ``vvp`` run itself is fast
(<100ms for 5000 cycles on the demo DUTs), so we don't cache its
output. Run-time knobs (seeds, clock periods) go to ``vvp`` as
plusargs, so sweeping them reuses one compiled image.

:func:`build_netlist` is the analyzer side of the multi-bit oracle
(rtl-buddy-cdc#331): it elaborates the *same* DUT source through Yosys,
with ``SIM_ORACLE_SYNTH`` defined so the DUT includes the plain-flop
stand-ins of ``meta_flop_synth.sv`` instead of the injecting models.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SIM_DIR = Path(__file__).parent
CACHE_DIR = SIM_DIR / ".cache"
#: Committed Yosys netlists of the multi-bit oracle DUTs, so the
#: analyzer half of each oracle runs without Yosys (rtl-buddy-cdc#331).
NETLIST_DIR = SIM_DIR / "netlists"

_RESULT_RE = re.compile(r"SIM_RESULT\s+errors=(\d+)\s+total=(\d+)")
_STATS_RE = re.compile(r"^SIM_STATS\s+(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class SimResult:
    """Outcome of a single ``vvp`` run."""

    errors: int
    total: int
    stdout: str
    #: ``key=value`` integers from an optional ``SIM_STATS`` line
    #: (``tb_bus_crossing.sv`` reports ``updates`` / ``src_changes``).
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total else 0.0


def iverilog_available() -> bool:
    return shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def _tb_for(dut_sv: Path) -> str:
    """Pick the testbench file matching the DUT's port shape.

    - ``dut_bus_*`` / ``dut_reconv_*`` DUTs: ``tb_bus_crossing.sv``
      (ports src_clk / dst_clk / inc / multi-bit q_out, plus the
      hierarchically-read ``src_data``; rtl-buddy-cdc#331).
    - ``*_rdc*`` / ``*_reset*`` DUTs: ``tb_reset_crossing.sv``
      (ports include global_rst_n + local_rst_req).
    - ``*clock_mux*`` DUTs: ``tb_clock_glitch.sv`` (ports include
      ck0_a / ck0_b / ck1 / sel_d / edge_count, no d_in/q_out pair).
    - Everything else: ``tb_crossing.sv``.
    """
    stem = dut_sv.stem.lower()
    if stem.startswith(("dut_bus_", "dut_reconv_")):
        return "tb_bus_crossing.sv"
    if "rdc" in stem:
        return "tb_reset_crossing.sv"
    if "clock_mux" in stem:
        return "tb_clock_glitch.sv"
    return "tb_crossing.sv"


def _digest(dut_sv: Path, macros: dict[str, str | int]) -> str:
    h = hashlib.sha256()
    for src in sorted(
        [dut_sv, SIM_DIR / _tb_for(dut_sv), SIM_DIR / "meta_flop_lib.sv"]
    ):
        h.update(src.read_bytes())
        h.update(b"\0")
    for k in sorted(macros):
        h.update(f"{k}={macros[k]}".encode())
        h.update(b"\0")
    return h.hexdigest()


def compile_dut(dut_sv: Path, macros: dict[str, str | int]) -> Path:
    """Compile ``dut_sv`` + tb + meta_flop_lib, return the vvp path."""
    if not iverilog_available():
        raise RuntimeError("iverilog / vvp not on PATH")
    CACHE_DIR.mkdir(exist_ok=True)
    key = _digest(dut_sv, macros)
    vvp_path = CACHE_DIR / f"{key}.vvp"
    if vvp_path.exists():
        return vvp_path
    cmd = ["iverilog", "-g2012", "-o", str(vvp_path), "-I", str(SIM_DIR)]
    for k, v in macros.items():
        cmd.extend(["-D", f"{k}={v}"])
    cmd.extend([str(dut_sv), str(SIM_DIR / _tb_for(dut_sv))])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"iverilog failed for {dut_sv}:\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )
    return vvp_path


def run(
    dut_sv: Path,
    macros: dict[str, str | int],
    plusargs: dict[str, int] | None = None,
) -> SimResult:
    """Compile and run the DUT; return parsed result.

    ``plusargs`` become ``+KEY=value`` arguments of the ``vvp`` run;
    they do not enter the compile cache key.
    """
    vvp_path = compile_dut(dut_sv, macros)
    args = [f"+{k}={v}" for k, v in (plusargs or {}).items()]
    proc = subprocess.run(
        ["vvp", str(vvp_path), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"vvp failed for {dut_sv}:\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )
    match = _RESULT_RE.search(proc.stdout)
    if not match:
        raise RuntimeError(f"vvp produced no SIM_RESULT line:\n{proc.stdout}")
    stats: dict[str, int] = {}
    for line in _STATS_RE.findall(proc.stdout):
        for item in line.split():
            key, _, value = item.partition("=")
            stats[key] = int(value)
    return SimResult(
        errors=int(match.group(1)),
        total=int(match.group(2)),
        stdout=proc.stdout,
        stats=stats,
    )


def yosys_available() -> bool:
    return shutil.which("yosys") is not None


def build_netlist(dut_sv: Path, out_json: Path) -> None:
    """Elaborate ``dut_sv`` (top = its stem) to a Yosys JSON netlist.

    Mirrors the ``lint`` front half (``proc; flatten; opt_clean``) and
    passes the commands through a ``-s`` script file, because CI's
    Yosys splits a ``-p`` script at commas. Yosys runs in ``SIM_DIR``
    on bare file names so the ``src`` attributes in the committed
    netlist carry no machine-specific path.
    """
    if not yosys_available():
        raise RuntimeError("yosys not on PATH")
    top = dut_sv.stem
    script = "\n".join(
        [
            f"read_verilog -sv -DSIM_ORACLE_SYNTH {dut_sv.name}",
            f"hierarchy -top {top}",
            "proc",
            "flatten",
            "opt_clean",
            f"write_json {out_json.resolve()}",
            "",
        ]
    )
    with tempfile.TemporaryDirectory(prefix="rbcdc-sim-") as tmp:
        ys = Path(tmp) / "build.ys"
        ys.write_text(script)
        proc = subprocess.run(
            ["yosys", "-q", "-s", str(ys)],
            capture_output=True,
            text=True,
            check=False,
            cwd=dut_sv.parent,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"yosys failed for {dut_sv}:\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )


def multibit_duts() -> list[Path]:
    """The ``tb_bus_crossing.sv`` DUTs, i.e. those with a netlist."""
    return sorted(
        p for p in SIM_DIR.glob("dut_*.sv") if _tb_for(p) == "tb_bus_crossing.sv"
    )


if __name__ == "__main__":
    # Regenerate the committed netlists after editing a multi-bit DUT:
    #     uv run python -m tests.sim.runner
    NETLIST_DIR.mkdir(exist_ok=True)
    for dut in multibit_duts():
        build_netlist(dut, NETLIST_DIR / f"{dut.stem}.json")
        print(f"built {NETLIST_DIR.name}/{dut.stem}.json")
