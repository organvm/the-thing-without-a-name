#!/usr/bin/env python3
"""Validate the versioned Danse release manifest at a named publication phase."""

from __future__ import annotations

_bootstrap_sys = __import__("sys")
_bootstrap_os = __import__("os")
if getattr(getattr(_bootstrap_os, "__spec__", None), "origin", None) not in {
    "built-in",
    "frozen",
}:
    raise RuntimeError("release checker requires the frozen OS path bootstrap")
_bootstrap_scripts = _bootstrap_os.path.realpath(
    _bootstrap_os.path.dirname(__file__)
)
_bootstrap_root = _bootstrap_os.path.dirname(_bootstrap_scripts)
_bootstrap_boundaries = [_bootstrap_root]
_bootstrap_argv_index = 0
_bootstrap_root_value = ""
_bootstrap_root_present = False
while _bootstrap_argv_index < len(_bootstrap_sys.argv):
    _bootstrap_argument = _bootstrap_sys.argv[_bootstrap_argv_index]
    if _bootstrap_argument in {"--r", "--ro", "--roo", "--root"}:
        _bootstrap_root_present = True
        if _bootstrap_argv_index + 1 < len(_bootstrap_sys.argv):
            _bootstrap_argv_index += 1
            _bootstrap_root_value = _bootstrap_sys.argv[_bootstrap_argv_index]
    elif any(
        _bootstrap_argument.startswith(prefix)
        for prefix in ("--r=", "--ro=", "--roo=", "--root=")
    ):
        _bootstrap_root_present = True
        _bootstrap_root_value = _bootstrap_argument.split("=", 1)[1]
    if _bootstrap_root_present:
        _bootstrap_boundaries.append(
            _bootstrap_os.path.realpath(
                _bootstrap_root_value or _bootstrap_os.getcwd()
            )
        )
        _bootstrap_root_value = ""
        _bootstrap_root_present = False
    _bootstrap_argv_index += 1
_bootstrap_prefix = _bootstrap_os.path.realpath(_bootstrap_sys.prefix)
_bootstrap_active_venv = _bootstrap_sys.prefix != _bootstrap_sys.base_prefix
_bootstrap_safe_path: list[str] = []
_bootstrap_argument = ""
_bootstrap_boundary = ""
_bootstrap_entry = ""
_bootstrap_candidate = ""
_bootstrap_common = ""
_bootstrap_inside_repository = False
_bootstrap_prefix_common = ""
for _bootstrap_entry in _bootstrap_sys.path:
    _bootstrap_candidate = _bootstrap_os.path.realpath(
        _bootstrap_entry or _bootstrap_os.getcwd()
    )
    _bootstrap_inside_repository = False
    for _bootstrap_boundary in _bootstrap_boundaries:
        try:
            _bootstrap_common = _bootstrap_os.path.commonpath(
                [_bootstrap_candidate, _bootstrap_boundary]
            )
        except ValueError:
            _bootstrap_common = ""
        if _bootstrap_common == _bootstrap_boundary:
            _bootstrap_inside_repository = True
            break
    try:
        _bootstrap_prefix_common = _bootstrap_os.path.commonpath(
            [_bootstrap_candidate, _bootstrap_prefix]
        )
    except ValueError:
        _bootstrap_prefix_common = ""
    if (
        _bootstrap_inside_repository
        and not (
            _bootstrap_active_venv
            and _bootstrap_prefix_common == _bootstrap_prefix
        )
    ):
        continue
    _bootstrap_safe_path.append(_bootstrap_entry)
_bootstrap_sys.path[:] = _bootstrap_safe_path
del (
    _bootstrap_active_venv,
    _bootstrap_argument,
    _bootstrap_argv_index,
    _bootstrap_boundaries,
    _bootstrap_boundary,
    _bootstrap_candidate,
    _bootstrap_common,
    _bootstrap_entry,
    _bootstrap_inside_repository,
    _bootstrap_os,
    _bootstrap_prefix,
    _bootstrap_prefix_common,
    _bootstrap_root,
    _bootstrap_root_present,
    _bootstrap_root_value,
    _bootstrap_safe_path,
    _bootstrap_scripts,
    _bootstrap_sys,
)

import argparse
import sys
from pathlib import Path

_release_scripts = str(Path(__file__).resolve().parent)
sys.path.insert(0, _release_scripts)
from release_contract import MANIFEST, PHASES, ROOT, ReleaseError, phase_blockers, validate_release
if _release_scripts in sys.path:
    sys.path.remove(_release_scripts)
del _release_scripts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--phase", choices=PHASES, default="draft")
    parser.add_argument(
        "--list-gates",
        action="store_true",
        help="after draft validation, list the still-open public and release predicates",
    )
    args = parser.parse_args()
    try:
        manifest = validate_release(args.root, manifest_path=args.manifest, phase=args.phase)
    except ReleaseError as exc:
        parser.exit(1, f"release manifest: FAIL - {exc}\n")

    public = phase_blockers(manifest, "public")
    release = phase_blockers(manifest, "release")
    print(
        f"release manifest: {manifest['release_id']} {manifest['version']} "
        f"verified for {args.phase}; public blockers={len(public)}, release blockers={len(release)}"
    )
    if args.list_gates:
        for blocker in public:
            print(f"  public: {blocker}")
        for blocker in release:
            if blocker not in public:
                print(f"  release-only: {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
