#!/usr/bin/env python3
"""leak-guard: block uploads and pushes that contain identifying strings.

Scans release artifacts (recursing into zip and gzip) or the git objects a push
would send, and refuses to pass when a configured "needle" appears.

Design rules:
- The needles live in a local file outside the repository. Neither the source
  code nor CI configuration contains them.
- Fail-closed: if the needle file is missing or malformed, nothing passes.
- Output never prints a needle, only file names, member names and counts.

Exit codes: 0 = clean, 1 = blocked (real hit), 2 = configuration or runtime error.
"""
import argparse
import base64
import gzip
import io
import os
import re
import subprocess
import sys
import zipfile

__version__ = "1.0.0"

ENV_NEEDLE_FILE = "LEAK_GUARD_NEEDLE_FILE"
DEFAULT_NEEDLE_FILE = os.path.join(os.path.expanduser("~"), ".config", "leak-guard", "needles.txt")

EXIT_CLEAN, EXIT_BLOCKED, EXIT_ERROR = 0, 1, 2

NEEDLE_RE = re.compile(r"[a-z0-9_.-]{4,}")
WORD = re.compile(rb"[A-Za-z0-9_]")
ZIP_MAGIC = b"PK\x03\x04"
GZ_MAGIC = b"\x1f\x8b"
MAX_DEPTH = 2
MAX_MEMBER_BYTES = 300 * 2 ** 20


class ConfigError(Exception):
    """Needle file missing or malformed. Messages never contain needle text."""


def load_needles(path=None):
    """Read needles (one per line, '#' comments allowed). Raises ConfigError."""
    path = path or os.environ.get(ENV_NEEDLE_FILE) or DEFAULT_NEEDLE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        raise ConfigError(f"needle file not readable: {path}")
    needles = []
    for no, line in enumerate(lines, 1):
        v = line.split("#", 1)[0].strip().lower()
        if not v:
            continue
        if not NEEDLE_RE.fullmatch(v):
            raise ConfigError(f"needle file line {no}: must be >= 4 chars of [a-z0-9_.-] (length {len(v)})")
        needles.append(v)
    if not needles:
        raise ConfigError(f"needle file has no needles: {path}")
    return needles


class Matcher:
    """Counts "real" hits of one needle in raw bytes.

    A real hit starts at a word boundary and is written all-lowercase,
    Capitalized or UPPERCASE. Hits in the middle of a word or in odd casing
    are treated as false positives. UTF-16LE text and base64 encodings of the
    three casings are also detected.
    """

    def __init__(self, needle):
        self.n = needle.encode()
        self.n16 = b"".join(bytes([c, 0]) for c in self.n)
        self.cases = {self.n, self.n.capitalize(), self.n.upper()}
        full = len(self.n) // 3 * 3
        self.b64 = sorted({base64.b64encode(c[:full]) for c in self.cases}) if full >= 3 else []

    def count(self, raw):
        low, hits = raw.lower(), 0
        for nd, step in ((self.n, 1), (self.n16, 2)):
            start = 0
            while True:
                i = low.find(nd, start)
                if i < 0:
                    break
                prev_is_word = i - step >= 0 and WORD.match(raw[i - step:i - step + 1])
                if not prev_is_word and raw[i:i + len(nd)][::step] in self.cases:
                    hits += 1
                start = i + 1
        for pattern in self.b64:
            hits += raw.count(pattern)
        return hits


class Scanner:
    def __init__(self, needles):
        self.matchers = [Matcher(n) for n in needles]

    def count(self, raw):
        return sum(m.count(raw) for m in self.matchers)

    def count_deep(self, raw, depth=0):
        """Hits in raw plus nested zip/gzip content. Returns (hits, member_paths)."""
        total, members = self.count(raw), []
        try:
            if raw[:4] == ZIP_MAGIC and depth < MAX_DEPTH:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    for info in z.infolist():
                        if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                            continue
                        c, sub = self.count_deep(z.read(info), depth + 1)
                        if c:
                            total += c
                            members.append(info.filename)
                            members.extend(info.filename + "!" + s for s in sub)
            elif raw[:2] == GZ_MAGIC and depth < MAX_DEPTH:
                c, _ = self.count_deep(gzip.decompress(raw), depth + 1)
                if c:
                    total += c
                    members.append("<gzip>")
        except Exception as ex:  # a corrupt archive is reported, not silently passed
            members.append(f"<container-error {type(ex).__name__}>")
        return total, members


def scan_files(paths, needles):
    """Returns (ok, findings). Findings hold file names, members and counts only."""
    scanner, findings = Scanner(needles), []
    for p in paths:
        with open(p, "rb") as f:
            hits, members = scanner.count_deep(f.read())
        if hits:
            findings.append(f"{os.path.basename(p)}: hits={hits} members={members[:3]}")
    return not findings, findings


def _git(repo, *args, data=None):
    r = subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed (rc={r.returncode}): {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def outgoing_objects(repo, ref, remote="origin"):
    """Object ids reachable from ref that the remote does not already have."""
    tips = sorted({line.split("\t")[0] for line in _git(repo, "ls-remote", remote).decode().splitlines() if "\t" in line})
    have = []
    if tips:
        out = _git(repo, "cat-file", "--batch-check", data=("\n".join(tips) + "\n").encode()).decode()
        have = [x[0] for x in (l.split() for l in out.splitlines()) if len(x) == 3 and x[1] in ("commit", "tag")]
    revs = [ref] + ["^" + h for h in have]
    out = _git(repo, "rev-list", "--objects", "--stdin", data=("\n".join(revs) + "\n").encode()).decode()
    oids = [line.split(" ", 1)[0] for line in out.splitlines() if line]
    sha = _git(repo, "rev-parse", "--verify", ref).decode().strip()
    if _git(repo, "cat-file", "-t", sha).decode().strip() == "tag" and sha not in oids:
        oids.insert(0, sha)  # annotated tag object itself
    return oids


def scan_git(repo, ref, needles, remote="origin"):
    """Scan every object a push of ref would send. Returns (ok, findings)."""
    oids = outgoing_objects(repo, ref, remote)
    scanner, findings = Scanner(needles), []
    if not oids:
        return True, findings
    proc = subprocess.run(["git", "-C", repo, "cat-file", "--batch"],
                          input=("\n".join(oids) + "\n").encode(), capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git cat-file --batch failed (rc={proc.returncode})")
    buf, pos = proc.stdout, 0
    while pos < len(buf):
        nl = buf.index(b"\n", pos)
        oid, kind, size = buf[pos:nl].decode().split()
        body = buf[nl + 1:nl + 1 + int(size)]
        pos = nl + 1 + int(size) + 1
        hits, members = scanner.count_deep(body)
        if hits:
            findings.append(f"{kind} {oid[:12]}: hits={hits}" + (f" members={members[:3]}" if members else ""))
    return not findings, findings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="leak-guard", description=__doc__.split("\n\n")[0])
    ap.add_argument("--needle-file", help=f"needle file (default: ${ENV_NEEDLE_FILE} or {DEFAULT_NEEDLE_FILE})")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("files", help="scan files (zip/gzip are opened recursively)")
    f.add_argument("paths", nargs="+")
    g = sub.add_parser("git", help="scan the objects a push of REF would send")
    g.add_argument("ref")
    g.add_argument("--repo", default=".")
    g.add_argument("--remote", default="origin")
    args = ap.parse_args(argv)

    try:
        needles = load_needles(args.needle_file)
        if args.cmd == "files":
            ok, findings = scan_files(args.paths, needles)
            scope = f"{len(args.paths)} file(s)"
        else:
            ok, findings = scan_git(args.repo, args.ref, needles, args.remote)
            scope = f"outgoing objects of {args.ref}"
    except (ConfigError, RuntimeError, OSError) as ex:
        print(f"ERROR {ex} -- not passing (fail-closed)", file=sys.stderr)
        return EXIT_ERROR

    if ok:
        print(f"OK clean: {scope}, {len(needles)} needle(s)")
        return EXIT_CLEAN
    for line in findings:
        print(f"BLOCK {line}")
    return EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
