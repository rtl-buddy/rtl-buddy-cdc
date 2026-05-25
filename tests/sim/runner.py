"""Python wrapper around Icarus Verilog for the sim oracle.

Given a DUT module name and a latency-cycles parameter, compile the
DUT + ``tb_crossing.sv`` + ``meta_flop_lib.sv`` with ``iverilog``,
run with ``vvp``, parse the single ``SIM_RESULT errors=N total=M``
line emitted at end-of-run, and return a :class:`SimResult`.

The compilation is cached by SHA-256 of the SV inputs + macros, so
repeated runs of the same DUT with the same macros are an in-memory
load + a fresh ``vvp`` invocation. The ``vvp`` run itself is fast
(<100ms for 5000 cycles on the demo DUTs), so we don't cache its
output.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SIM_DIR = Path(__file__).parent
CACHE_DIR = SIM_DIR / ".cache"

_RESULT_RE = re.compile(r"SIM_RESULT\s+errors=(\d+)\s+total=(\d+)")


@dataclass(frozen=True)
class SimResult:
    """Outcome of a single ``vvp`` run."""

    errors: int
    total: int
    stdout: str

    @property
    def error_rate(self) -> float:
        return self.errors / self.total if self.total else 0.0


def iverilog_available() -> bool:
    return shutil.which("iverilog") is not None and shutil.which("vvp") is not None


def _digest(dut_sv: Path, macros: dict[str, str | int]) -> str:
    h = hashlib.sha256()
    for src in sorted(
        [dut_sv, SIM_DIR / "tb_crossing.sv", SIM_DIR / "meta_flop_lib.sv"]
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
    # Route DUTs to their matching testbench. The reset-aware DUTs
    # have a different port list (global_rst_n + local_rst_req) so
    # they pair with tb_reset_crossing.sv; everything else uses the
    # plain tb_crossing.sv.
    tb_name = (
        "tb_reset_crossing.sv" if "rdc" in dut_sv.stem.lower() else "tb_crossing.sv"
    )
    cmd.extend([str(dut_sv), str(SIM_DIR / tb_name)])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"iverilog failed for {dut_sv}:\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}\n"
        )
    return vvp_path


def run(dut_sv: Path, macros: dict[str, str | int]) -> SimResult:
    """Compile and run the DUT; return parsed result."""
    vvp_path = compile_dut(dut_sv, macros)
    proc = subprocess.run(
        ["vvp", str(vvp_path)], capture_output=True, text=True, check=False
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
    return SimResult(
        errors=int(match.group(1)),
        total=int(match.group(2)),
        stdout=proc.stdout,
    )
