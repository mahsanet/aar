#!/usr/bin/env python3
"""
build_aar.py — Build a Go project for Android and package it as an AAR.

Note: This just does a plain go build. This doesn't build a library or generate bindings.
"""

import argparse
import io
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


# ─── ABI targets ──────────────────────────────────────────────────────────────
# (ndk_triple_prefix, GOARCH, abi_dir)
TARGETS = [
    ("aarch64-linux-android",    "arm64", "arm64-v8a"),
    ("armv7a-linux-androideabi", "arm",   "armeabi-v7a"),
    ("x86_64-linux-android",     "amd64", "x86_64"),
    ("i686-linux-android",       "386",   "x86"),
]


# ─── Helpers ──────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def detect_ndk_host() -> str:
    system = platform.system()
    machine = platform.machine()
    if system == "Linux" and machine == "x86_64":
        return "linux-x86_64"
    if system == "Darwin":
        # NDK only ships darwin-x86_64; runs via Rosetta on Apple Silicon
        return "darwin-x86_64"
    die(f"Unsupported host: {system}-{machine}")


def resolve_ndk(ndk_home: str | None) -> Path:
    path = ndk_home or os.environ.get("NDK_HOME") or os.environ.get("ANDROID_NDK_HOME")
    if not path:
        die("NDK path not set. Pass --ndk-home or export NDK_HOME / ANDROID_NDK_HOME.")
    p = Path(path)
    if not p.is_dir():
        die(f"NDK directory not found: {p}")
    return p


def make_classes_jar() -> bytes:
    """Return the bytes of a minimal classes.jar with a proper META-INF/MANIFEST.MF."""
    manifest = (
        "Manifest-Version: 1.0\r\n"
        "Created-By: build_aar.py\r\n"
        "\r\n"
    ).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("META-INF/MANIFEST.MF", manifest)
    return buf.getvalue()


def make_manifest_xml(package: str, min_sdk: int) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f'    package="{package}"\n'
        '    android:versionCode="1"\n'
        '    android:versionName="1.0">\n'
        f'    <uses-sdk android:minSdkVersion="{min_sdk}" />\n'
        '</manifest>\n'
    )


# ─── Build ────────────────────────────────────────────────────────────────────
def build_sos(
    ndk_root: Path,
    ndk_host: str,
    min_sdk: int,
    lib_name: str,
    build_cmd: str,
    ldflags: str,
    jni_dir: Path,
) -> None:
    clang_dir = ndk_root / "toolchains" / "llvm" / "prebuilt" / ndk_host / "bin"

    for triple_prefix, goarch, abi in TARGETS:
        ndk_target = f"{triple_prefix}{min_sdk}"
        clang = clang_dir / f"{ndk_target}-clang"
        if not clang.is_file():
            die(f"Clang not found: {clang}")

        out_so = jni_dir / abi / f"{lib_name}.so"
        out_so.parent.mkdir(parents=True, exist_ok=True)

        log(f"Building {lib_name}.so for {abi} (GOARCH={goarch})")

        cmd = [
            "go", "build",
            "-o", str(out_so),
            "-trimpath",
            "-ldflags", ldflags,
            "-buildvcs=false",
            build_cmd,
        ]

        env = os.environ.copy()
        env.update({
            "CC":          str(clang),
            "CGO_ENABLED": "1",
            "GOOS":        "android",
            "GOARCH":      goarch,
        })

        result = subprocess.run(cmd, env=env)
        if result.returncode != 0:
            die(f"go build failed for {abi} (exit {result.returncode})")

        log(f"  → {out_so}")


# ─── Package ──────────────────────────────────────────────────────────────────
def package_aar(
    jni_dir: Path,
    package_name: str,
    min_sdk: int,
    aar_out: Path,
) -> None:
    log(f"Packaging {aar_out}...")
    aar_out.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(aar_out, mode="w", compression=zipfile.ZIP_DEFLATED) as aar:
        # AndroidManifest.xml
        aar.writestr("AndroidManifest.xml", make_manifest_xml(package_name, min_sdk))

        # classes.jar
        aar.writestr("classes.jar", make_classes_jar())

        # R.txt (required by some AGP versions)
        aar.writestr("R.txt", "")

        # jni/<abi>/lib*.so
        for so in sorted(jni_dir.rglob("*.so")):
            arc_name = Path("jni") / so.relative_to(jni_dir)
            aar.write(so, arc_name)


def pack_sos_with_upx(jni_dir: Path) -> None:
    upx = shutil.which("upx")
    if not upx:
        die("UPX not found on PATH. Install UPX before building the -upx AAR.")

    so_files = sorted(jni_dir.rglob("*.so"))
    if not so_files:
        die(f"No shared libraries found to pack in {jni_dir}")

    for so in so_files:
        log(f"Packing {so} with UPX")
        result = subprocess.run([upx, str(so)])
        if result.returncode != 0:
            die(f"UPX failed for {so} (exit {result.returncode})")


# ─── CLI ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build a Go project for Android and package it as an AAR.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--lib-name",     default="",    help="Output library base name (without extension)")
    p.add_argument("--package-name", default="",    help="Android package name for AndroidManifest.xml")
    p.add_argument("--build-cmd",    default="",    help="Go build target passed to 'go build'")
    p.add_argument("--min-sdk",      default=21,    type=int,   help="Minimum Android API level")
    p.add_argument("--out-dir",      default=None,  help="Output directory (default: ./out)")
    p.add_argument("--ndk-home",     default=None,  help="Path to NDK root (overrides NDK_HOME / ANDROID_NDK_HOME)")
    p.add_argument(
        "--ldflags",
        default="-s -w -buildid=",
        help="Flags passed to 'go build -ldflags'. Wrap in quotes if they contain spaces.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd() / "out"
    ndk_root = resolve_ndk(args.ndk_home)
    ndk_host = detect_ndk_host()

    log(f"NDK root : {ndk_root}")
    log(f"NDK host : {ndk_host}")
    log(f"Min SDK  : {args.min_sdk}")
    log(f"ldflags  : {args.ldflags}")

    with tempfile.TemporaryDirectory() as tmp:
        jni_dir = Path(tmp) / "jni"
        jni_dir.mkdir()

        build_sos(
            ndk_root=ndk_root,
            ndk_host=ndk_host,
            min_sdk=args.min_sdk,
            lib_name=args.lib_name,
            build_cmd=args.build_cmd,
            ldflags=args.ldflags,
            jni_dir=jni_dir,
        )

        aar_out = out_dir / f"{args.lib_name}.aar"
        package_aar(
            jni_dir=jni_dir,
            package_name=args.package_name,
            min_sdk=args.min_sdk,
            aar_out=aar_out,
        )

        upx_jni_dir = Path(tmp) / "jni-upx"
        shutil.copytree(jni_dir, upx_jni_dir)
        pack_sos_with_upx(upx_jni_dir)

        upx_aar_out = out_dir / f"{args.lib_name}-upx.aar"
        package_aar(
            jni_dir=upx_jni_dir,
            package_name=args.package_name,
            min_sdk=args.min_sdk,
            aar_out=upx_aar_out,
        )

if __name__ == "__main__":
    main()
