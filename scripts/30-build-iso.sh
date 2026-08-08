#!/bin/bash
# Build the installer ISO from an NFVIS ISO.
#
# Strategy: reuse the NFVIS media wholesale as both the package repo AND the
# Anaconda installer, replacing only the kickstart and adding our payload.
# That guarantees the exact kernel the out-of-tree modules are pinned to
# (AlmaLinux 8.9 point releases are no longer on public mirrors), and reuses a
# known-good UEFI+BIOS El Torito layout.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
. "$HERE/lib.sh"

SRC_ISO="${1:?usage: 30-build-iso.sh <nfvis.iso> <workdir> <output.iso> [--full]}"
WORK="${2:?}"; OUT="${3:?}"; MODE="${4:-}"
LABEL="ENCS_SW_89"

say "Extracting $(basename "$SRC_ISO")"
rm -rf "$WORK/iso"; mkdir -p "$WORK/iso"
bsdtar -C "$WORK/iso" -xf "$SRC_ISO" 2>/dev/null || die "extraction failed"
chmod -R u+w "$WORK/iso"

if [ "$MODE" != "--full" ]; then
    say "Resolving the minimal package set"
    python3 "$HERE/20-resolve-packages.py" "$WORK/iso" "$WORK/keep.txt"

    say "Trimming Packages/"
    before=$(du -sm "$WORK/iso/Packages" | cut -f1)
    ( cd "$WORK/iso/Packages" && ls ./*.rpm 2>/dev/null | sed 's|^\./||' | sort > "$WORK/have.txt" )
    sort "$WORK/keep.txt" > "$WORK/keep.sorted"
    comm -23 "$WORK/have.txt" "$WORK/keep.sorted" > "$WORK/drop.txt"
    info "dropping $(wc -l < "$WORK/drop.txt"), keeping $(wc -l < "$WORK/keep.sorted")"
    ( cd "$WORK/iso/Packages" && xargs -a "$WORK/drop.txt" -r rm -f )
    info "Packages/: ${before}MB -> $(du -sm "$WORK/iso/Packages" | cut -f1)MB"

    say "Regenerating repodata"
    # comps.xml AND modules.yaml both live INSIDE repodata/ - copy them out
    # before deleting it, or they are silently lost. python38 (needed by
    # nic-xl710-i350 for /usr/bin/python3.8) is a MODULAR package: without
    # modules.yaml dnf can refuse to see it.
    COMPS=$(ls "$WORK/iso"/repodata/*comps.xml 2>/dev/null | head -1 || true)
    MODS=$(ls "$WORK/iso"/repodata/*modules.yaml 2>/dev/null | head -1 || true)
    [ -n "$COMPS" ] && cp -f "$COMPS" "$WORK/comps.xml"
    [ -n "$MODS" ]  && cp -f "$MODS"  "$WORK/modules.yaml"
    rm -rf "$WORK/iso/repodata"
    createrepo_c --quiet ${COMPS:+-g "$WORK/comps.xml"} "$WORK/iso"
    if [ -n "$MODS" ]; then
        modifyrepo_c --mdtype=modules "$WORK/modules.yaml" "$WORK/iso/repodata" \
            || die "failed to re-add modules.yaml"
        grep -q 'type="modules"' "$WORK/iso/repodata/repomd.xml" \
            || die "modules metadata missing from repomd.xml"
        info "modules.yaml re-added and verified"
    fi
fi

say "Installing kickstart and payload"
rm -f "$WORK/iso"/{ks.cfg,iso_upgrade_ks.cfg,anaconda-ks.cfg,base_rpm.list,fake_dmidecode_data.bin}
rm -f "$WORK/iso/isolinux/anaconda-ks.cfg"
rm -rf "$WORK/iso"/{upgrade,kickstart_scripts,tmp}
cp "$ROOT/kickstart/ks-encs.cfg" "$WORK/iso/ks-encs.cfg"
rm -rf "$WORK/iso/encs"; mkdir -p "$WORK/iso/encs"
cp -a "$ROOT/payload/." "$WORK/iso/encs/"
# Strip build pollution. Running py_compile on the payload (e.g. a syntax
# check) leaves __pycache__ behind, and that bytecode is both useless and
# wrong for the target - it is whatever Python the BUILD host had, not el8's.
find "$WORK/iso/encs" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$WORK/iso/encs" -name '*.pyc' -delete 2>/dev/null || true

if [ -n "${ROOT_PASSWORD:-}" ]; then
    info "setting root password from \$ROOT_PASSWORD"
    sed -i "s|^rootpw --plaintext .*|rootpw --plaintext ${ROOT_PASSWORD}|" "$WORK/iso/ks-encs.cfg"
else
    warn "using the default root password 'encs' - set ROOT_PASSWORD to change it"
fi

say "Writing boot configuration"
cat > "$WORK/iso/EFI/BOOT/grub.cfg" <<EOF
set default="0"
set timeout=15
search --no-floppy --set=root -l '$LABEL'

menuentry 'Install AlmaLinux 8.9 + ENCS5412 switch bootstrap  (ERASES DISK)' {
    linuxefi /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=$LABEL inst.ks=hd:LABEL=$LABEL:/ks-encs.cfg console=tty0 console=ttyS0,9600 inst.text quiet
    initrdefi /images/pxeboot/initrd.img
}
menuentry 'Install with manual disk selection (interactive)' {
    linuxefi /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=$LABEL console=tty0 console=ttyS0,9600 inst.text
    initrdefi /images/pxeboot/initrd.img
}
menuentry 'Rescue an existing system' {
    linuxefi /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=$LABEL rd.live.ram rescue quiet
    initrdefi /images/pxeboot/initrd.img
}
EOF
# BIOS boot is optional: an EFI-only source ISO has no isolinux/ at all, and
# writing into a missing directory would abort the build.
if [ -d "$WORK/iso/isolinux" ]; then
cat > "$WORK/iso/isolinux/isolinux.cfg" <<EOF
default vesamenu.c32
timeout 150
menu title AlmaLinux 8.9 - ENCS 5412 switch bootstrap

label install
  menu label ^Install (ERASES DISK)
  menu default
  kernel vmlinuz
  append initrd=initrd.img inst.stage2=hd:LABEL=$LABEL inst.ks=hd:LABEL=$LABEL:/ks-encs.cfg console=tty0 console=ttyS0,9600 inst.text quiet

label manual
  menu label Install with ^manual disk selection
  kernel vmlinuz
  append initrd=initrd.img inst.stage2=hd:LABEL=$LABEL console=tty0 console=ttyS0,9600 inst.text

label rescue
  menu label ^Rescue an existing system
  kernel vmlinuz
  append initrd=initrd.img inst.stage2=hd:LABEL=$LABEL rd.live.ram rescue quiet
EOF
else
    warn "no isolinux/ on the source ISO - building UEFI-only (no BIOS boot)"
fi

say "Building $OUT"
rm -f "$OUT"
BIOSARGS=()
if [ -f "$WORK/iso/isolinux/isolinux.bin" ]; then
    BIOSARGS=(-b isolinux/isolinux.bin -c isolinux/boot.cat
              -no-emul-boot -boot-load-size 4 -boot-info-table -eltorito-alt-boot)
fi
xorriso -as mkisofs -o "$OUT" -V "$LABEL" -J -R -joliet-long \
    "${BIOSARGS[@]}" \
    -e images/efiboot.img -no-emul-boot \
    "$WORK/iso" 2>&1 | grep -vE '^xorriso : UPDATE' || true

[ -f "$OUT" ] || die "ISO was not produced"
say "Done"
ls -la "$OUT"
sha256sum "$OUT"
