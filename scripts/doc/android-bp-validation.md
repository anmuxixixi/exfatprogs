# Android.bp lightweight validation

## Goal

The Android build system normally requires a large AOSP checkout and builds a
global module dependency graph.  exfatprogs uses only a small subset of Soong's
`Android.bp` syntax, so a full Android source tree is unnecessary for a quick
pre-release compile check.

The lightweight validator follows this pipeline:

1. Find the `Android.bp` files below the exfatprogs source tree.
2. Parse the supported `cc_*` modules and resolve their `defaults`, source files,
   header libraries, static libraries, and explicitly mapped shared libraries.
3. Generate a small Ninja build graph for the requested executable and its
   static dependencies.
4. Invoke Clang and `llvm-ar` through Ninja.
5. Fail on unsupported module types or properties instead of silently ignoring
   them.

This is intended to catch changes such as missing source files, invalid module
references, unsupported Blueprint properties, compilation failures, and link
failures.  It is not a complete replacement for a Soong build inside AOSP.

## Files

- `scripts/android_bp_to_ninja.py`: parses the supported Blueprint subset and
  generates `build.ninja`.
- `.github/workflows/android-integration.yml`: runs the validator with the
  Android NDK on a GitHub-hosted runner.

## Prerequisites for a local Linux check

On Debian or Ubuntu:

```sh
sudo apt-get update
sudo apt-get install -y clang ninja-build python3 libblkid-dev
```

Run all commands below from the exfatprogs repository root.

## Quick host-build validation

Keep all generated files under `android_out` in the repository root.  The
directory is ignored by Git:

```sh
out="$PWD/android_out"

python3 scripts/android_bp_to_ninja.py \
  --root . \
  --out "$out" \
  --cc clang \
  --ar ar \
  --target x86_64-linux-gnu \
  --module fsck.exfat

ninja -f "$out/build.ninja" -v
file "$out/bin/fsck.exfat"
```

For `mkfs.exfat`, map the Soong shared-library name to the host `libblkid`
linker argument and provide its exported header directory:

```sh
out="$PWD/android_out"

python3 scripts/android_bp_to_ninja.py \
  --root . \
  --out "$out" \
  --cc 'clang -I/usr/include/blkid' \
  --ar ar \
  --target x86_64-linux-gnu \
  --module mkfs.exfat \
  --shared-lib libext2_blkid=-lblkid

ninja -f "$out/build.ninja" -v
file "$out/bin/mkfs.exfat"
ldd "$out/bin/mkfs.exfat"
```

After inspecting the result, the complete local build can be removed by
deleting that one output directory:

```sh
rm -rf -- "$out"
```

The generated Ninja file sets `builddir` to `android_out`, so `.ninja_log`,
`.ninja_deps`, objects, libraries, and binaries all remain below that directory.

## Android NDK validation

Set `ANDROID_NDK_HOME` to an installed NDK and build with its LLVM toolchain:

```sh
toolchain="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64"
out="$PWD/android_out"

python3 scripts/android_bp_to_ninja.py \
  --root . \
  --out "$out" \
  --cc "$toolchain/bin/clang" \
  --ar "$toolchain/bin/llvm-ar" \
  --target aarch64-linux-android24 \
  --module fsck.exfat

ninja -f "$out/build.ninja" -v
"$toolchain/bin/llvm-readelf" --file-header "$out/bin/fsck.exfat"
```

The resulting ELF header should report AArch64.  The NDK alone is sufficient
for `fsck.exfat`, but it does not provide AOSP modules such as
`libext2_blkid`.  A true Android build of `mkfs.exfat` therefore also requires
compatible Android headers and a library for that module, or an occasional
full Soong integration build.

## Validating selected modules

`--module` may be repeated to generate rules for more than one executable:

```sh
python3 scripts/android_bp_to_ninja.py \
  --root . \
  --out "$out" \
  --cc clang \
  --ar ar \
  --target x86_64-linux-gnu \
  --module fsck.exfat \
  --module dump.exfat
```

If no `--module` option is supplied, the script attempts to generate all
supported buildable modules.  Explicit module selection is recommended for CI
because external Soong shared libraries must be mapped deliberately.

## Scope and limitations

- The parser intentionally supports only the `Android.bp` subset used by
  exfatprogs; it is not a general Blueprint or Soong implementation.
- Product variables, namespaces, generated sources, APEX packaging, and the
  complete Android module graph are outside its scope.
- `--shared-lib NAME=LINK_ARG` maps linking only.  Any headers exported by that
  Soong library must also be made visible to Clang for a host check.
- A successful host build validates the graph and C compilation, but it does
  not prove Android ABI compatibility.  The NDK build in CI provides that
  additional check for modules without AOSP-only dependencies.
