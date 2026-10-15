#!/usr/bin/env python3
"""iStoreOS 25.12 x86_64 EFI build orchestrator.

The GitHub Actions workflow only prepares the run (checkout, disk expansion,
cache). Everything else - dependency installation, source clone, patching,
feeds setup, seed assembly, compilation, artifact collection, gh-pages OTA
manifest and the rolling GitHub release - is driven from this script so the
pipeline is reproducible outside CI as well.

Stages can be run individually for debugging, e.g.:

    scripts/build_istoreos.py deps
    scripts/build_istoreos.py source
    scripts/build_istoreos.py all

Environment variables (all optional):
    ISTOREOS_BUILD_ROOT  Base directory for the working tree (default: ./work)
    ISTOREOS_JOBS        Parallel make jobs (default: number of CPUs)
    ISTOREOS_REPO        Source repository (default: istoreos/istoreos)
    ISTOREOS_BRANCH      Source branch (default: istoreos-25.12)
    GITHUB_REPOSITORY    owner/repo, used to build release + OTA URLs
    GH_TOKEN             Token used by the ``gh`` CLI for releases
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
OUT_DIR = REPO_ROOT / "out"

BUILD_ROOT = Path(os.environ.get("ISTOREOS_BUILD_ROOT", REPO_ROOT / "work")).resolve()
WORK_DIR = BUILD_ROOT / "istoreos"
SRC_DIR = WORK_DIR / "source"
QUARANTINE_DIR = WORK_DIR / "quarantine"

SOURCE_REPO = os.environ.get("ISTOREOS_REPO", "https://github.com/istoreos/istoreos.git")
SOURCE_BRANCH = os.environ.get("ISTOREOS_BRANCH", "istoreos-25.12")

TARGET_DIR = SRC_DIR / "bin" / "targets" / "x86" / "64"

RELEASE_TAG = "v25.12.5"
RELEASE_NAME = "iStoreOS-v25.12.5"
DEVICE_KEY = "x86_64"

SEED_CONFIG = CONFIG_DIR / "x86_64_efi.config"
SEED_PACKAGES = CONFIG_DIR / "x86_64_efi.packages"
FEEDS_CONF = CONFIG_DIR / "feeds.conf"
FEEDS_REMOVE = CONFIG_DIR / "feeds.remove"


def jobs(default_cpu: bool = True) -> int:
    env = os.environ.get("ISTOREOS_JOBS")
    if env:
        return int(env)
    return os.cpu_count() or 2 if default_cpu else 1


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"\n\033[1;32m==> {msg}\033[0m", flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
        check: bool = True) -> subprocess.CompletedProcess:
    printable = " ".join(cmd)
    print(f"$ {printable}", flush=True)
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=merged)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {printable}")
    return proc


def read_non_comment_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def replace_in_file(path: Path, old: str, new: str) -> int:
    text = path.read_text()
    count = text.count(old)
    if count:
        path.write_text(text.replace(old, new))
    return count


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #

# Host build dependencies for a full x86_64 image build. This is the union of
# the OpenWrt documented prerequisite set and the extras iStoreOS packages need
# (32-bit multilib for GRUB/img helpers, host dev libraries for various tools).
APT_PACKAGES = [
    # core toolchain and build essentials
    "autoconf", "automake", "bc", "binutils", "bison", "build-essential",
    "bzip2", "ccache", "cmake", "cpio", "diffutils", "file", "flex",
    "g++", "g++-multilib", "gawk", "gcc-multilib", "gettext", "git",
    "gperf", "libtool", "libtool-bin", "libltdl-dev", "patch", "pkg-config",
    "rsync", "scons", "subversion", "swig", "texinfo", "time",
    # 32-bit runtime/dev support (x86_64 target builds some 32-bit helpers)
    "lib32gcc-s1", "libc6-dev-i386",
    # libraries used by host tools
    "libelf-dev", "libfuse-dev", "libglib2.0-dev", "libgnutls28-dev",
    "liblzma-dev", "libncurses-dev", "libreadline-dev", "libssl-dev",
    "libzstd-dev", "zlib1g-dev", "zstd",
    # misc host utilities
    "ca-certificates", "curl", "device-tree-compiler", "jq", "libplist-utils",
    "nasm", "unzip", "wget", "xz-utils", "zip",
    # python
    "python3", "python3-dev", "python3-pip", "python3-pyelftools",
    "python3-setuptools", "python3-venv",
]


def stage_deps() -> None:
    log("Installing build dependencies")
    run(["sudo", "apt-get", "update", "-y"], env={"DEBIAN_FRONTEND": "noninteractive"})
    run(
        ["sudo", "apt-get", "install", "-y", "--no-install-recommends", *APT_PACKAGES],
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )


def stage_source() -> None:
    log(f"Cloning {SOURCE_REPO} ({SOURCE_BRANCH})")
    SRC_DIR.parent.mkdir(parents=True, exist_ok=True)
    if (SRC_DIR / ".git").is_dir():
        run(["git", "fetch", "--depth", "1", "origin", SOURCE_BRANCH], cwd=SRC_DIR)
        run(["git", "checkout", "-f", "FETCH_HEAD"], cwd=SRC_DIR)
    else:
        run([
            "git", "clone", "--depth", "1", "--branch", SOURCE_BRANCH,
            SOURCE_REPO, str(SRC_DIR),
        ])
    run(["git", "config", "user.email", "ci@localhost"], cwd=SRC_DIR)
    run(["git", "config", "user.name", "iStoreOS CI"], cwd=SRC_DIR)


def stage_patch() -> None:
    """Apply the source customisations that are not part of initrd/CI tooling."""
    log("Patching sources (default IP, OTA replacement, BUILD_DATE)")

    # 1) Default LAN IP: 10.0.0.1. iStoreOS rewrites the upstream defaults at
    #    package-install time via a sed in this Makefile, so changing the sed
    #    target is enough to move the whole default (login page, config_generate,
    #    the flash page, ...).
    istoreos_files_mk = SRC_DIR / "package" / "istoreos-files" / "Makefile"
    n = replace_in_file(istoreos_files_mk, "192.168.100.1", "10.0.0.1")
    if n == 0:
        raise SystemExit("default IP patch: expected marker not found in istoreos-files/Makefile")
    print(f"  istoreos-files/Makefile: replaced {n} occurrence(s) of 192.168.100.1 -> 10.0.0.1")

    # 2) os-release must expose BUILD_DATE (plain var) for sbwml's luci-app-ota.
    os_release = SRC_DIR / "package" / "base-files" / "files" / "usr" / "lib" / "os-release"
    text = os_release.read_text()
    # Match a real assignment; a plain substring test is fooled by the
    # existing OPENWRT_BUILD_DATE line.
    has_build_date = any(line.startswith("BUILD_DATE=") for line in text.splitlines())
    if not has_build_date:
        os_release.write_text(text.rstrip("\n") + '\nBUILD_DATE="%B"\n')
        print("  os-release: added BUILD_DATE=\"%B\"")

    # 3) Remove the in-tree iStoreOS OTA so the sbwml copy from the
    #    openwrt_pkgs feed is the only provider of luci-app-ota.
    native_ota = SRC_DIR / "package" / "diy" / "luci-app-ota"
    if native_ota.exists():
        dest = QUARANTINE_DIR / "source" / "package" / "diy" / "luci-app-ota"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(native_ota), str(dest))
        print(f"  quarantined {native_ota.relative_to(SRC_DIR)}")


def stage_feeds() -> None:
    log("Setting up feeds")
    shutil.copyfile(FEEDS_CONF, SRC_DIR / "feeds.conf")
    run(["./scripts/feeds", "update", "-a"], cwd=SRC_DIR)

    log("Resolving feed package name conflicts")
    _resolve_conflicts()

    # 'feeds update' builds one index per feed before the conflicting copies
    # are moved aside. Those indexes still advertise the quarantined packages,
    # and 'feeds install' resolves a source package by scanning the indexes in
    # feeds.conf order. It would therefore select the now-empty baseline feed,
    # create a dangling symlink and mark the name as installed, leaving the
    # intended provider feed uninstalled (e.g. sing-box/xray-core/microsocks
    # reported as "does not exist"). Rebuilding the indexes after the
    # quarantine makes the provider feed the only match.
    log("Rebuilding feed indexes after conflict resolution")
    run(["./scripts/feeds", "update", "-i"], cwd=SRC_DIR)

    run(["./scripts/feeds", "install", "-a"], cwd=SRC_DIR)


def _resolve_conflicts() -> None:
    """Move same-named package directories out of every feed except the
    designated provider feed, as described in config/feeds.remove."""
    conflicts: dict[str, str] = {}
    for line in read_non_comment_lines(FEEDS_REMOVE):
        name, provider = line.split()
        conflicts[name] = provider

    # Only the feeds declared in feeds.conf carry package directories. The
    # build system also drops a "feeds/base" symlink onto the core package/
    # tree; walking it would reach unrelated core packages (and, once feeds
    # install has run, loop back through package/feeds), so skip anything that
    # is not a declared feed or is reached through a symlink.
    declared = {line.split()[1] for line in read_non_comment_lines(FEEDS_CONF)}

    feeds_dir = SRC_DIR / "feeds"
    moved = 0
    for feed in sorted(p for p in feeds_dir.iterdir()
                       if p.is_dir() and p.name in declared and not p.is_symlink()):
        matches = [
            d for d in feed.rglob("*")
            if d.is_dir() and d.name in conflicts
            and conflicts[d.name] != feed.name
            and not d.is_symlink()
            and ".git" not in d.parts
            and (d / "Makefile").exists()
        ]
        for pkgdir in matches:
            dest = QUARANTINE_DIR / "feeds" / feed.name / pkgdir.relative_to(feed)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pkgdir), str(dest))
            print(f"  quarantined feeds/{feed.name}/{pkgdir.relative_to(feed)}")
            moved += 1
    print(f"  moved {moved} conflicting package director(ies)")


def stage_config() -> None:
    log("Assembling .config from seed")
    lines: list[str] = []
    lines.append("# Generated by scripts/build_istoreos.py - do not edit")
    lines.append(SEED_CONFIG.read_text().rstrip("\n"))
    lines.append("")
    lines.append("# --- packages ---")
    for pkg in read_non_comment_lines(SEED_PACKAGES):
        lines.append(f"CONFIG_PACKAGE_{pkg}=y")
    lines.append("")
    (SRC_DIR / ".config").write_text("\n".join(lines) + "\n")

    # Point the OTA updater at our gh-pages manifest by seeding a
    # uci-defaults script into the image overlay.
    api_url = _ota_api_url()
    if api_url:
        overlay = SRC_DIR / "files" / "etc" / "uci-defaults"
        overlay.mkdir(parents=True, exist_ok=True)
        (overlay / "99-set-ota-api").write_text(
            "#!/bin/sh\n"
            f"uci -q set ota.config.api_url='{api_url}'\n"
            "uci -q commit ota\n"
            "exit 0\n"
        )
        (overlay / "99-set-ota-api").chmod(0o755)
        print(f"  OTA api_url -> {api_url}")

    run(["make", "defconfig"], cwd=SRC_DIR)


def stage_build() -> None:
    log("Building firmware")
    build_date = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))
    env = {"SOURCE_DATE_EPOCH": str(build_date)}
    (WORK_DIR / "build-date").write_text(str(build_date))
    run(["make", f"-j{jobs()}", "download"], cwd=SRC_DIR, env=env)
    run(["make", f"-j{jobs()}"], cwd=SRC_DIR, env=env)


def stage_collect() -> None:
    log("Collecting artifacts")
    if not TARGET_DIR.is_dir():
        raise SystemExit(f"target directory not found: {TARGET_DIR}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Prefer the flashable combined-EFI image; fall back to any image.
    images = sorted(TARGET_DIR.glob("*combined-efi*.img.gz")) or sorted(TARGET_DIR.glob("*.img.gz"))
    if not images:
        raise SystemExit(f"no .img.gz images produced in {TARGET_DIR}")

    index = {
        "tag": RELEASE_TAG,
        "name": RELEASE_NAME,
        "device": DEVICE_KEY,
        "build_date": int((WORK_DIR / "build-date").read_text().strip()),
        "commit": _git_head(SRC_DIR),
        "files": [],
    }

    for img in images:
        dest = OUT_DIR / img.name
        shutil.copy2(img, dest)
        sha = _sha256(dest)
        index["files"].append({
            "name": img.name,
            "size": dest.stat().st_size,
            "sha256": sha,
        })
        # sha256sums file so users can verify locally.
        (OUT_DIR / f"{img.name}.sha256").write_text(f"{sha}  {img.name}\n")
        print(f"  {img.name}  {sha}")

    (OUT_DIR / "artifact-index.json").write_text(json.dumps(index, indent=2) + "\n")


def stage_release() -> None:
    log(f"Publishing release {RELEASE_TAG}")
    index = _load_index()
    assets = [OUT_DIR / f["name"] for f in index["files"]]
    notes = _release_notes(index)
    notes_file = OUT_DIR / "release-notes.md"
    notes_file.write_text(notes)

    listing = run(["gh", "release", "view", RELEASE_TAG], check=False)
    if listing.returncode != 0:
        run(["gh", "release", "create", RELEASE_TAG,
             "--title", RELEASE_NAME, "--notes-file", str(notes_file),
             *[str(a) for a in assets]])
    else:
        run(["gh", "release", "upload", RELEASE_TAG,
             *[str(a) for a in assets], "--clobber"])


def stage_ota() -> None:
    log("Updating gh-pages OTA manifest")
    index = _load_index()
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise SystemExit("GITHUB_REPOSITORY is required to build OTA URLs")

    entries = []
    for f in index["files"]:
        url = f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{f['name']}"
        entries.append({
            "build_date": str(index["build_date"]),
            "sha256sum": f["sha256"],
            "url": url,
            "logs": _ota_logs(index),
        })
    manifest = {DEVICE_KEY: entries}

    pages_dir = WORK_DIR / "gh-pages"
    if pages_dir.exists():
        shutil.rmtree(pages_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / "ota.json").write_text(json.dumps(manifest, indent=2) + "\n")

    remote = f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{repo}.git"
    run(["git", "init", "-q"], cwd=pages_dir)
    run(["git", "checkout", "-q", "-b", "gh-pages"], cwd=pages_dir)
    run(["git", "config", "user.email", "ci@localhost"], cwd=pages_dir)
    run(["git", "config", "user.name", "iStoreOS CI"], cwd=pages_dir)
    run(["git", "add", "ota.json"], cwd=pages_dir)
    run(["git", "commit", "-q", "-m", f"ota: {RELEASE_TAG} ({index['build_date']})"], cwd=pages_dir)
    run(["git", "push", "--force", remote, "gh-pages"], cwd=pages_dir)


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head(path: Path) -> str:
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(path),
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _load_index() -> dict:
    path = OUT_DIR / "artifact-index.json"
    if not path.is_file():
        raise SystemExit(f"missing {path}; run the collect stage first")
    return json.loads(path.read_text())


def _ota_api_url() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return ""
    return f"https://raw.githubusercontent.com/{repo}/gh-pages/ota.json"


def _release_notes(index: dict) -> str:
    stamp = dt.datetime.utcfromtimestamp(index["build_date"]).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = "\n".join(
        f"- `{f['name']}` ({f['size'] // 1024 // 1024} MiB) sha256 `{f['sha256']}`"
        for f in index["files"]
    )
    return (
        f"# {RELEASE_NAME}\n\n"
        f"- Built: {stamp}\n"
        f"- Source commit: `{index['commit']}`\n"
        f"- Default LAN address: http://10.0.0.1\n\n"
        f"## Artifacts\n\n{rows}\n"
    )


def _ota_logs(index: dict) -> str:
    stamp = dt.datetime.utcfromtimestamp(index["build_date"]).strftime("%Y-%m-%d %H:%M")
    return f"iStoreOS {RELEASE_TAG} x86_64 EFI ({stamp} UTC), source {index['commit']}"


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

STAGES = {
    "deps": stage_deps,
    "source": stage_source,
    "patch": stage_patch,
    "feeds": stage_feeds,
    "config": stage_config,
    "build": stage_build,
    "collect": stage_collect,
    "release": stage_release,
    "ota": stage_ota,
}

ORDER = ["deps", "source", "patch", "feeds", "config", "build", "collect"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["all", *STAGES], nargs="?", default="all")
    args = parser.parse_args()

    print(f"repo={REPO_ROOT}")
    print(f"build_root={BUILD_ROOT}")
    print(f"jobs={jobs()}")

    if args.stage == "all":
        for name in ORDER:
            STAGES[name]()
    else:
        STAGES[args.stage]()


if __name__ == "__main__":
    main()
