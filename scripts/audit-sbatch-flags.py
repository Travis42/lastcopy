#!/usr/bin/env python3
"""audit-sbatch-flags.py — flag-matrix audit for sbatch/shell bundles (incident 2026-10-10, job 20150).

Class of bug this fences: an sbatch script passes a flag a CLI does not define,
and the CLI's argument guard rejects it late (stage N of a long run) — losing the
run. Usage: run from repo root; exit 1 on any REAL finding.

  python3 scripts/audit-sbatch-flags.py [--root .]

Cross-checks every --flag used after a repo console-CLI token in slurm-v2/*.sbatch
and scripts/*.sh against add_argument() definitions in the CLI's module.
"""
import argparse, glob, os, re, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    pp = open(os.path.join(root, "pyproject.toml")).read()
    entries = re.findall(r'(\w[\w-]*)\s*=\s*"lastcopy\.([\w.]+):(\w+)"', pp)
    valid = {}
    for cli, mod, fn in entries:
        path = os.path.join(root, "lastcopy", *mod.split(".")) + ".py"
        if not os.path.exists(path):
            continue
        src = open(path, errors="replace").read()
        valid[cli] = set(re.findall(r'add_argument\(\s*["\'](--[\w-]+)', src))

    files = sorted(glob.glob(os.path.join(root, "slurm-v2", "*.sbatch"))
                   + glob.glob(os.path.join(root, "slurm-v2", "*.sh"))
                   + glob.glob(os.path.join(root, "scripts", "*.sh")))
    findings = 0
    for f in files:
        text = open(f, errors="replace").read()
        lines = text.splitlines()
        for i, ln in enumerate(lines, 1):
            # continuation lines: flags may span backslash-continued commands
            block = ln
            j = i
            while j < len(lines) and lines[j - 1].rstrip().endswith("\\"):
                block += " " + lines[j]; j += 1
            for cli, flags in valid.items():
                for m in re.finditer(rf'(^|[/\s]){cli}\s', block):
                    # attribute only flags AFTER the cli token (avoids git --oneline false positives)
                    tail = block[m.end():]
                    used = set(re.findall(r'(--[\w-]+)', tail))
                    bad = used - flags - {"--help"}
                    if bad:
                        findings += 1
                        print(f"REAL: {os.path.relpath(f, root)}:{i} {cli} -> {sorted(bad)}")
    print(f"scanned {len(files)} scripts, {len(valid)} CLIs — real findings: {findings}")
    sys.exit(1 if findings else 0)

if __name__ == "__main__":
    main()
