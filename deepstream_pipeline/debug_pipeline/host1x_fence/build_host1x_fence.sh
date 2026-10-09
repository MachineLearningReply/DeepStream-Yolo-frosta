#!/usr/bin/env bash
# Builds NVIDIA's host1x-fence driver module with the spin_lock_irqsave fix (see README.md), for L4T 36.4.7.
# Builds only this one module, on the Jetson, against the installed kernel headers and NVIDIA's installed
# OOT module symbols. Checks the result. Installs NOTHING: it prints the install/rollback commands at the end.
#
#   bash ~/DeepStream-Yolo-frosta/deepstream_pipeline/debug_pipeline/host1x_fence/build_host1x_fence.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$HERE/host1x-fence-irqsave.patch"
TAG="jetson_36.4.7"
REPO_URL="https://gitlab.com/nvidia/nv-tegra/linux-nv-oot.git"   # NVIDIA's official GitLab mirror of nv-tegra
WORK="$HOME/host1x-fence-build"
SRC="$WORK/linux-nv-oot"
KDIR="/lib/modules/$(uname -r)/build"
OOT_SYMVERS="/usr/src/nvidia/nvidia-oot/Module.symvers"

step() { echo; echo "=== $*"; }
fail() { echo; echo "STOP: $*" >&2; exit 1; }

step "Pre-checks"
for tool in git patch make gcc modinfo; do
    command -v "$tool" > /dev/null || fail "'$tool' is missing (sudo apt-get install -y git patch build-essential)"
done
grep -q "REVISION: 4.7" /etc/nv_tegra_release || fail "this patch/build is for L4T 36.4.7; found: $(head -1 /etc/nv_tegra_release)"
[ -d "$KDIR" ] || fail "kernel build kit missing: $KDIR (package nvidia-l4t-kernel-headers)"
[ -f "$OOT_SYMVERS" ] || fail "NVIDIA OOT symbols missing: $OOT_SYMVERS (package nvidia-l4t-kernel-oot-headers)"
INSTALLED="$(modinfo -n host1x-fence)" || fail "host1x-fence module not found"
echo "  installed module: $INSTALLED"
echo "  kernel:           $(uname -r)"
if grep -q "^CONFIG_MODULE_SIG_FORCE=y" "$KDIR/.config" 2>/dev/null; then
    fail "the kernel only loads signed modules (CONFIG_MODULE_SIG_FORCE=y); a self-built module would be refused"
fi
echo "  module signatures enforced: no"

step "Source: $TAG from $REPO_URL"
mkdir -p "$WORK"
if [ ! -d "$SRC/.git" ]; then
    git clone --depth 1 -b "$TAG" "$REPO_URL" "$SRC"
fi
echo "  $(git -C "$SRC" describe --tags --always)  $(git -C "$SRC" log -1 --format=%cd)"

# dev.c includes <nvidia/conftest.h> only for two macros that concern Linux >= 6.2 (class_create/devnode
# signature changes). On this 5.15 kernel both must be undefined, i.e. an empty header.
mkdir -p "$WORK/conftest/nvidia"
echo "/* empty: dev.c only checks NV_CLASS_* macros for Linux >= 6.2; this kernel is $(uname -r) */" > "$WORK/conftest/nvidia/conftest.h"

build() {   # $1 = output folder name, $2 = "patched" or "original"
    local out="$WORK/$1"
    rm -rf "$out"
    cp -r "$SRC/drivers/gpu/host1x-fence" "$out"
    if [ "$2" = "patched" ]; then
        (cd "$out" && patch -p4 < "$PATCH")   # patch paths start with a/drivers/gpu/host1x-fence/ (4 parts)
    fi
    make -C "$KDIR" M="$out" \
        KBUILD_EXTRA_SYMBOLS="$OOT_SYMVERS" \
        KCFLAGS="-I$WORK/conftest -I$SRC/include -I$SRC/drivers/gpu/host1x/include" \
        modules > "$out/build.log" 2>&1 || { tail -30 "$out/build.log"; fail "build of the $2 module failed (log: $out/build.log)"; }
    echo "  built: $out/host1x-fence.ko"
}

check_vermagic() {
    local want got
    want="$(modinfo -F vermagic host1x-fence)"
    got="$(modinfo -F vermagic "$1")"
    [ "$want" = "$got" ] || fail "vermagic differs: installed '$want', new '$got'"
    echo "  vermagic matches: $got"
}

# Every symbol a module imports carries a CRC; the kernel refuses to load the module if a CRC doesn't match.
# Check each one against the running kernel's and NVIDIA's OOT module symbol lists.
check_crcs() {
    local missing=0 crc sym ref
    while read -r crc sym; do
        ref="$(awk -v s="$sym" '$2 == s {print $1; exit}' "$KDIR/Module.symvers" "$OOT_SYMVERS")"
        if [ -z "$ref" ]; then
            echo "  ? $sym: not found in the symbol lists"; missing=1
        elif [ "$(printf '%d' "$ref")" != "$(printf '%d' "$crc")" ]; then
            echo "  ! $sym: module $crc, kernel $ref"; missing=1
        fi
    done < <(modprobe --dump-modversions "$1")
    [ "$missing" = 0 ] || fail "symbol checksums don't match the running kernel/drivers for $1"
    echo "  all $(modprobe --dump-modversions "$1" | wc -l) symbol checksums match the running kernel and NVIDIA drivers"
}

step "1/2 Build the UNCHANGED module and compare it with the installed one"
build original original
check_vermagic "$WORK/original/host1x-fence.ko"
if diff <(modprobe --dump-modversions "$INSTALLED" | sort) <(modprobe --dump-modversions "$WORK/original/host1x-fence.ko" | sort) > /dev/null; then
    echo "  imported symbols and checksums are identical to the installed module"
else
    diff <(modprobe --dump-modversions "$INSTALLED" | sort) <(modprobe --dump-modversions "$WORK/original/host1x-fence.ko" | sort) || true
    fail "the unchanged build differs from the installed module (see above); not continuing"
fi

step "2/2 Build the PATCHED module"
build patched patched
grep -c "spin_lock_irqsave" "$WORK/patched/dev.c" | sed 's/^/  spin_lock_irqsave sites in dev.c: /'
check_vermagic "$WORK/patched/host1x-fence.ko"
check_crcs "$WORK/patched/host1x-fence.ko"
cp "$WORK/patched/host1x-fence.ko" "$WORK/host1x-fence.ko.patched"
sha256sum "$WORK/host1x-fence.ko.patched"

cat <<EOF

Done. Nothing was installed. The patched module is:
  $WORK/host1x-fence.ko.patched

Install (see README.md; keep the original backup ~/host1x-fence.ko.orig):
  [ -f ~/host1x-fence.ko.orig ] || cp "$INSTALLED" ~/host1x-fence.ko.orig
  sudo cp "$WORK/host1x-fence.ko.patched" "$INSTALLED"
  sudo depmod -a
  lsinitramfs /boot/initrd | grep -q host1x-fence && sudo nv-update-initrd
  sudo reboot

Rollback:
  sudo cp ~/host1x-fence.ko.orig "$INSTALLED" && sudo depmod -a
  lsinitramfs /boot/initrd | grep -q host1x-fence && sudo nv-update-initrd
  sudo reboot
EOF
