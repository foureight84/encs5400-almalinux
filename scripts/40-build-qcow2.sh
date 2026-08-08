#!/bin/bash
# Run the installer ISO once under QEMU/KVM to produce a ready-to-import qcow2.
#
# The result is a pre-installed disk - no Anaconda stage2, no initrd - which is
# both far smaller than an installer ISO and a much nicer Proxmox workflow
# (import disk, boot, done).
#
# A successful run is also an end-to-end test of the ISO: if the kickstart is
# broken, this fails rather than shipping something that only looks correct.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"

ISO="${1:?usage: 40-build-qcow2.sh <installer.iso> <workdir> <output.qcow2>}"
WORK="${2:?}"; OUT="${3:?}"
SIZE="${QCOW_SIZE:-16G}"; MEM="${QCOW_MEM:-4096}"; CPUS="${QCOW_CPUS:-4}"
LOG="$WORK/install-console.log"

CODE=$(ovmf_code); VARS=$(ovmf_vars)
[ -n "$CODE" ] && [ -n "$VARS" ] || die "OVMF firmware not found (apt install ovmf)"
mkdir -p "$WORK"
cp -f "$VARS" "$WORK/OVMF_VARS.fd"

ACCEL=$(check_kvm)
# -cpu host only exists with a hardware accelerator; under TCG qemu rejects it
# ("The 'host' CPU model is not supported in TCG"). Fall back to 'max'.
if [ "$ACCEL" = "kvm" ]; then CPUMODEL=host; else CPUMODEL=max; fi
say "Installing under QEMU (accel=$ACCEL, cpu=$CPUMODEL, console -> $LOG)"
rm -f "$LOG" "$WORK/raw.qcow2"
qemu-img create -f qcow2 "$WORK/raw.qcow2" "$SIZE" >/dev/null

# -no-reboot: the kickstart ends in 'reboot', which then exits QEMU.
# SMBIOS product satisfies switch-confd's dmidecode platform gate. NOTE: the
# value must not contain a comma - QEMU's -smbios parser splits on commas
# before honouring quotes, so "Cisco Systems, Inc." is rejected outright.
set +e
timeout "${QCOW_TIMEOUT:-5400}" qemu-system-x86_64 \
    -machine "q35,accel=$ACCEL" -cpu "$CPUMODEL" -smp "$CPUS" -m "$MEM" \
    -drive "if=pflash,format=raw,readonly=on,file=$CODE" \
    -drive "if=pflash,format=raw,file=$WORK/OVMF_VARS.fd" \
    -drive "file=$WORK/raw.qcow2,format=qcow2,if=virtio,cache=unsafe" \
    -drive "file=$ISO,media=cdrom,readonly=on" \
    -boot order=d \
    -smbios type=1,manufacturer=Cisco,product=ENCS5412/K9 \
    -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
    -display none -serial "file:$LOG" -monitor none -no-reboot
rc=$?
set -e
info "qemu exited rc=$rc"

if ! grep -aqiE 'Installation complete|Running post-installation' "$LOG" 2>/dev/null; then
    echo >&2
    echo "Install did not complete. Tail of the console log:" >&2
    tail -40 "$LOG" >&2
    echo >&2
    echo "Full log: $LOG" >&2
    exit 1
fi

say "Compacting -> $OUT"
rm -f "$OUT"
qemu-img convert -c -O qcow2 "$WORK/raw.qcow2" "$OUT"
qemu-img info "$OUT" | head -5
sha256sum "$OUT"
