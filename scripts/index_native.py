#!/usr/bin/env python3
"""Index native code: every shared object's dynamic symbols, so library coupling stops being invisible.

Usage: index_native.py <extracted_root> <out_dir>

Writes:
  native.tsv    package, lib, kind, symbol

`kind` is what the symbol means for coupling:

  jni       a `Java_*` entry point - this lib serves Java of the enclosing package, i.e. the names the APK's
            `System.loadLibrary` / `native` declarations will bind to. These are the ones an app actually calls.
  export    another defined dynamic symbol (T/D/W/B) - callable from elsewhere.
  import    an undefined symbol (U) - something this lib expects at load time, so the lib or the platform must
            provide it. This is the edge that matters most: an `import` naming another package's lib is real coupling
            that the manifest graph cannot see at all.

Only names, never disassembly. `nm -D` (override with $NM) reads ELF, including the arm/arm64 libs inside the APKs
this image ships, so no NDK is needed. Symbol *versions* are stripped.

Note on the package column: only the system image is indexed, so an app that has been updated into /data appears as its
component-less stub. An empty package means a platform lib with no APK beside it (e.g. /system/lib).
"""
import os
import re
import subprocess
import sys
from pathlib import Path

NM = os.environ.get("NM") or "nm"
# `nm -D` output is "<addr> <type> <name>", or "         U <name>" for undefined.
LINE = re.compile(r"^(?:[0-9a-fA-F]+)?\s*([A-Za-z])\s+(\S+)$")
# Symbol versions arrive as `name@@LIB_1.0`; the base name is what links.
VERSION = re.compile(r"@.*$")


def symbols(lib):
    try:
        r = subprocess.run([NM, "-D", lib], capture_output=True, text=True)
    except OSError:
        return
    if r.returncode != 0:
        return
    for line in r.stdout.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        kind, name = m.group(1), VERSION.sub("", m.group(2))
        if not name:
            continue
        if kind == "U":
            yield "import", name
        elif name.startswith("Java_") or name.startswith("JNI_OnLoad"):
            yield "jni", name
        elif kind in "TtDdBbWwVv":
            yield "export", name


def resolve(lib, root):
    """Where the bytes actually are.

    App-embedded libs in this image are *absolute* symlinks into the platform (`.../lib/arm/libx.so ->
    /system/lib/libx.so`), which dangle on a Mac. Map the device path back into the extracted tree so those libs are
    read at all - without this every app lib is skipped and only the vendor/system_ext ones get indexed, which is
    exactly the "native code is invisible" defect. Returns None when the target really is missing.
    """
    if lib.is_file():
        return lib
    try:
        target = os.readlink(lib)
    except OSError:
        return None
    if not target.startswith("/"):
        cand = (lib.parent / target).resolve()
        return cand if cand.is_file() else None
    # /system/lib/libx.so and /vendor/lib64/libx.so both land under the partition dir named in the path.
    for cand in (root / target.lstrip("/"), *(root / part / target.lstrip("/") for part in ("system", "vendor", "product"))):
        if cand.is_file():
            return cand
    return None


def enclosing_package(lib, root):
    """The APK a lib sits inside, if any: walk up until a directory holds one."""
    d = Path(lib).parent
    while True:
        if d == root.parent:
            return ""
        try:
            apks = [p for p in d.iterdir() if p.suffix == ".apk"]
        except OSError:
            apks = []
        if apks:
            return apks[0].stem
        if d == root or d.parent == d:
            return ""
        d = d.parent


def main() -> None:
    root = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    # Not filtered by is_file(): the app-embedded libs are symlinks and is_file() is False for a dangling one on a
    # Mac, so filtering here would drop exactly the app libs this index exists to surface. resolve() decides instead.
    libs = sorted(p for p in root.rglob("*.so") if p.is_file() or p.is_symlink())
    n = 0
    with open(out / "native.tsv", "w") as f:
        f.write("package\tlib\tkind\tsymbol\n")
        skipped = 0
        for lib in libs:
            real = resolve(lib, root)
            if real is None:
                skipped += 1
                continue
            pkg = enclosing_package(lib, root)
            rel = str(lib.relative_to(root))
            for kind, name in symbols(str(real)):
                f.write(f"{pkg}\t{rel}\t{kind}\t{name}\n")
                n += 1
    print(f"{len(libs)} libs seen, {len(libs) - skipped} read, {skipped} unresolved, {n} symbols indexed")


if __name__ == "__main__":
    main()
