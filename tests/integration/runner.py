"""Fetch, build and analyse the open-source integration corpus.

The corpus (rtl-buddy-cdc#331) is a list of real multi-clock designs
written by other people, run through the analyzer with pinned
expectations. Their RTL is **not** vendored: ``corpus.toml`` names,
per design, the upstream repository, a full commit SHA, the licence,
the source files and the top module; this module fetches that one
commit on demand. The SDC files under ``sdc/`` and the expectations in
the manifest are this project's own work and are committed.

Safety
~~~~~~

A fetched repository is untrusted data. It is cloned into its own
directory with hooks disabled and only read: its source files are
handed to Yosys / the analyzer as paths. Nothing in it is executed
(no Makefiles, scripts or build systems), and the Yosys script is
written outside it.

Cache
~~~~~

``$RB_CDC_INTEGRATION_CACHE`` if set, else ``tests/integration/.cache/``
(gitignored)::

    <cache>/src/<owner>-<repo>-<sha12>/    shallow checkout of the pinned
                                           commit
    <cache>/netlists/<name>-<digest>.json  Yosys netlist, keyed by SHA-256
                                           of the Yosys version, the script
                                           and the source bytes

``rm -rf`` the directory to reset it.

Command line
~~~~~~~~~~~~

``uv run python -m tests.integration.runner [NAME ...] [--slang]`` builds
the named designs (all by default) and prints what the tests compare:
cell / flop counts, clocks, crossings, per-rule findings, and with
``--slang`` the ``lint --frontend slang`` verdict. Use it to review a
new design's findings before pinning them in the manifest.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from rtl_buddy_cdc.cli import app

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "corpus.toml"
CACHE_ENV = "RB_CDC_INTEGRATION_CACHE"
DEFAULT_CACHE = HERE / ".cache"

# Every design must stay small: the corpus is about coding styles, not
# scale, and it runs on laptops and shared CI runners.
MAX_CELLS = 20_000
# Wall-clock budget per Yosys build. The corpus designs take well under
# a second each; a build that runs this long is a regression.
YOSYS_TIMEOUT_S = 300
GIT_TIMEOUT_S = 300

CLASSIFICATIONS = frozenset({"true_positive", "false_positive", "outside_model"})
GAP_KINDS = frozenset({"false_positive", "false_negative", "frontend", "crash"})
# SPDX ids the licence rule admits (see corpus.toml).
PERMISSIVE_LICENSES = frozenset(
    {
        "MIT",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "Apache-2.0",
        "ISC",
        "CC0-1.0",
        "SHL-0.51",
        "SHL-2.1",
        "CERN-OHL-P-2.0",
    }
)


# --- Manifest ---------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """``count`` reviewed findings of rule ``rule``."""

    rule: str
    count: int
    classification: str
    reason: str
    gap: str = ""


@dataclass(frozen=True)
class Missed:
    """``count`` findings of ``rule`` the analyzer should have produced."""

    rule: str
    count: int
    gap: str
    reason: str


@dataclass(frozen=True)
class Gap:
    name: str
    kind: str
    rules: tuple[str, ...]
    summary: str
    reproducer: str


@dataclass(frozen=True)
class SlangExpectation:
    """``lint --frontend slang`` on the design's sources. When
    ``matches_yosys`` is false, ``crossings`` / ``rules`` pin the slang
    verdict and ``gap`` names the cause."""

    reads: bool
    matches_yosys: bool
    rules: dict[str, int] | None = None
    crossings: int | None = None
    gap: str = ""


@dataclass(frozen=True)
class Design:
    name: str
    description: str
    repo: str
    commit: str
    license: str
    license_file: str
    sources: tuple[str, ...]
    local_sources: tuple[Path, ...]
    incdirs: tuple[str, ...]
    top: str
    sdc: Path
    clocks: tuple[str, ...]
    crossings: int
    rules: dict[str, int]
    domain_unknown: int
    findings: tuple[Finding, ...]
    missed: tuple[Missed, ...]
    slang: SlangExpectation
    license_note: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Manifest:
    designs: tuple[Design, ...]
    gaps: dict[str, Gap]


def load(path: Path = MANIFEST) -> Manifest:
    data = tomllib.loads(path.read_text())
    gaps = {
        name: Gap(
            name=name,
            kind=g["kind"],
            rules=tuple(g["rules"]),
            summary=g["summary"].strip(),
            reproducer=g["reproducer"].strip(),
        )
        for name, g in data.get("gaps", {}).items()
    }
    designs: list[Design] = []
    for d in data["design"]:
        expect = d["expect"]
        designs.append(
            Design(
                name=d["name"],
                description=d["description"].strip(),
                repo=d["repo"],
                commit=d["commit"],
                license=d["license"],
                license_file=d["license_file"],
                sources=tuple(d["sources"]),
                local_sources=tuple(HERE / s for s in d.get("local_sources", ())),
                incdirs=tuple(d.get("incdirs", ())),
                top=d["top"],
                sdc=HERE / d["sdc"],
                clocks=tuple(expect["clocks"]),
                crossings=expect["crossings"],
                rules=dict(expect.get("rules", {})),
                domain_unknown=expect.get("domain_unknown", 0),
                findings=tuple(Finding(**f) for f in d.get("finding", ())),
                missed=tuple(Missed(**m) for m in d.get("missed", ())),
                slang=SlangExpectation(**d["slang"]),
                license_note=d.get("license_note", ""),
                notes=d.get("notes", ""),
            )
        )
    return Manifest(designs=tuple(designs), gaps=gaps)


def load_manifest(path: Path = MANIFEST) -> list[Design]:
    return list(load(path).designs)


# --- Fetch ------------------------------------------------------------------


def cache_root() -> Path:
    env = os.environ.get(CACHE_ENV)
    return Path(env).expanduser().resolve() if env else DEFAULT_CACHE


def source_dir(design: Design) -> Path:
    """One checkout per (repository, commit), shared by the designs that
    take different files from it."""
    repo = design.repo.rstrip("/").removesuffix(".git").rsplit("/", 2)
    return cache_root() / "src" / f"{repo[-2]}-{repo[-1]}-{design.commit[:12]}"


def _git(args: list[str], cwd: Path) -> str:
    # Hooks off: the fetched repository is untrusted, and nothing in it
    # may run. ``protocol.file.allow=never`` keeps a hostile URL from
    # reaching the local filesystem.
    cmd = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-c",
        "advice.detachedHead=false",
        *args,
    ]
    # No credential prompt: a vanished repository answers like a private
    # one, and a prompt would hang the test run.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n{proc.stderr}")
    return proc.stdout.strip()


def is_cached(design: Design) -> bool:
    d = source_dir(design)
    if not (d / ".git").is_dir():
        return False
    try:
        return _git(["rev-parse", "HEAD"], d) == design.commit
    except RuntimeError:
        return False


def fetch(design: Design) -> Path:
    """Shallow-fetch ``design.commit`` (once) and return the checkout.

    Verifies the checked-out SHA and that the recorded licence file is
    present at that commit.
    """
    d = source_dir(design)
    if not is_cached(design):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        _git(["init", "-q"], d)
        _git(["remote", "add", "origin", design.repo], d)
        _git(["fetch", "-q", "--depth", "1", "origin", design.commit], d)
        _git(["checkout", "-q", "FETCH_HEAD"], d)
    head = _git(["rev-parse", "HEAD"], d)
    if head != design.commit:
        raise RuntimeError(
            f"{design.name}: checked out {head}, expected {design.commit}"
        )
    if not (d / design.license_file).is_file():
        raise RuntimeError(
            f"{design.name}: licence file {design.license_file} missing at "
            f"{design.commit}"
        )
    return d


@functools.cache
def network_available(host: str = "github.com", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


# --- Build ------------------------------------------------------------------


def yosys_available() -> bool:
    return shutil.which("yosys") is not None


@functools.cache
def yosys_version() -> str:
    proc = subprocess.run(["yosys", "-V"], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def yosys_script(design: Design, out: str) -> str:
    """The ``lint --frontend yosys`` recipe. Upstream sources are named
    relative to the checkout (Yosys runs there), so the netlist's
    ``src`` attributes do not depend on where the cache lives; this
    repository's own ``local_sources`` (wrappers) are absolute."""
    inc = "".join(f"-I {i} " for i in design.incdirs)
    srcs = [*design.sources, *(p.as_posix() for p in design.local_sources)]
    return "\n".join(
        [
            f"read_verilog -sv {inc}{' '.join(srcs)}",
            f"hierarchy -top {design.top}",
            "proc",
            "flatten",
            "opt_clean",
            f"write_json {out}",
            "",
        ]
    )


def build(design: Design, src: Path) -> Path:
    """Build the flattened netlist (cached) and return its path.

    The script goes through ``yosys -s`` from a file outside the
    checkout, not ``-p``: some Yosys releases (0.52, the one GitHub's
    ``ubuntu-latest`` apt installs) split a ``-p`` argument at commas.
    """
    h = hashlib.sha256()
    h.update(yosys_version().encode())
    h.update(yosys_script(design, "<out>").encode())
    for rel in design.sources:
        h.update(rel.encode())
        h.update((src / rel).read_bytes())
    for local in design.local_sources:
        h.update(local.name.encode())
        h.update(local.read_bytes())
    digest = h.hexdigest()
    out = cache_root() / "netlists" / f"{design.name}-{digest[:16]}.json"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.partial.json")
    with tempfile.TemporaryDirectory() as d:
        ys = Path(d) / "build.ys"
        ys.write_text(yosys_script(design, str(tmp)))
        proc = subprocess.run(
            ["yosys", "-q", "-s", str(ys)],
            cwd=src,
            capture_output=True,
            text=True,
            timeout=YOSYS_TIMEOUT_S,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"yosys failed for {design.name}:\n--- stdout ---\n{proc.stdout}"
            f"\n--- stderr ---\n{proc.stderr}"
        )
    tmp.replace(out)
    return out


def cell_count(netlist_path: Path, top: str) -> int:
    modules = json.loads(netlist_path.read_text())["modules"]
    return len(modules[top].get("cells", {}))


# --- Analyse ----------------------------------------------------------------

_RUNNER = CliRunner()


@dataclass(frozen=True)
class Result:
    exit_code: int
    report: dict[str, Any]

    @property
    def rules(self) -> dict[str, int]:
        c = Counter(v["rule_id"] for v in self.report["violations"])
        return dict(sorted(c.items()))

    @property
    def crossings(self) -> int:
        return int(self.report["summary"]["crossings"])

    @property
    def flop_clocks(self) -> set[str]:
        return {d["clock"] for d in self.report["domains"] if d.get("clock")}

    @property
    def domain_unknown(self) -> int:
        return int(self.report["summary"]["domain_unknown"])


def _invoke(args: list[str]) -> Result:
    result = _RUNNER.invoke(app, args)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    report: dict[str, Any] = {}
    if result.exit_code in (0, 1):
        report = json.loads(result.stdout)
    return Result(result.exit_code, report)


def analyze(design: Design, netlist_path: Path) -> Result:
    return _invoke(
        ["analyze", "-n", str(netlist_path), "-s", str(design.sdc), "-f", "json"]
    )


def lint_slang(design: Design, src: Path) -> Result:
    """``lint --frontend slang`` on the same sources. Exit 2 (the
    elaboration failed) comes back as a :class:`Result` with an empty
    report, not an exception."""
    args = ["lint", "--frontend", "slang", "--top", design.top]
    args += ["-s", str(design.sdc), "-f", "json"]
    for inc in design.incdirs:
        args += ["-I", str(src / inc)]
    args += [str(src / s) for s in design.sources]
    args += [str(p) for p in design.local_sources]
    return _invoke(args)


# --- CLI --------------------------------------------------------------------


def _peak_child_rss_mb() -> float:
    """Largest resident set of any child process so far (Yosys)."""
    import resource
    import sys

    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # bytes on macOS, KiB on Linux
    return rss / (1 << 20) if sys.platform == "darwin" else rss / 1024


def _summarise(design: Design, slang: bool) -> None:
    src = fetch(design)
    nl = build(design, src)
    res = analyze(design, nl)
    s = res.report["summary"]
    print(f"== {design.name}  ({design.repo} @ {design.commit[:12]})")
    print(
        f"   cells={cell_count(nl, design.top)} flops={s['flops']} "
        f"exit={res.exit_code} peak_child_rss={_peak_child_rss_mb():.0f}MB"
    )
    print(
        f"   flop clocks={sorted(res.flop_clocks)} domain_unknown={res.domain_unknown}"
    )
    print(f"   crossings={res.crossings} rules={res.rules}")
    for v in res.report["violations"]:
        print(f"     {v['rule_id']:8} {v.get('cell_name', '')}: {v['message'][:160]}")
    if slang:
        sl = lint_slang(design, src)
        if sl.exit_code not in (0, 1):
            print(f"   slang: exit {sl.exit_code} (did not elaborate)")
        else:
            print(f"   slang: crossings={sl.crossings} rules={sl.rules}")
            for v in sl.report["violations"]:
                print(
                    f"     {v['rule_id']:8} {v.get('cell_name', '')}: {v['message'][:160]}"
                )


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--slang", action="store_true")
    args = ap.parse_args(argv)
    designs = load_manifest()
    if args.names:
        designs = [d for d in designs if d.name in set(args.names)]
    for design in designs:
        _summarise(design, args.slang)


if __name__ == "__main__":
    main()
