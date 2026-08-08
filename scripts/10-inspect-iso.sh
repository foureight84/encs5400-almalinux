#!/bin/bash
# Validate that an NFVIS ISO can actually produce a working ENCS 5412 image.
#
# The critical check is switch_firmware.bin. Cisco REMOVED that file in NFVIS
# 4.16+ - everything else (the kernel module, the loader, the platform gate)
# is byte-identical, but without the firmware blob the Marvell ASIC has no OS
# to run and the switch is inert. An ISO that fails this check is unusable no
# matter what else it contains.
set -euo pipefail
. "$(dirname "$0")/lib.sh"

ISO="${1:?usage: 10-inspect-iso.sh <nfvis.iso>}"
[ -f "$ISO" ] || die "no such file: $ISO"

say "Inspecting $(basename "$ISO")"

LIST=$(bsdtar -tf "$ISO" 2>/dev/null) || die "cannot read $ISO - is it an ISO?"

# --- version -------------------------------------------------------------
VER=$(bsdtar -xOf "$ISO" version 2>/dev/null | head -3 || true)
if [ -n "$VER" ]; then
    printf '%s\n' "$VER" | sed 's/^/    /'
else
    warn "no 'version' file - this may not be an NFVIS ISO"
fi

NFVIS_VER=$(printf '%s' "$VER" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
[ -n "$NFVIS_VER" ] && info "detected NFVIS $NFVIS_VER"

# --- required packages ---------------------------------------------------
# NB: match a DIGIT after the name. Plain "switch-confd" also matches
# switch-confd-debugsource and switch-confd-debuginfo, and picking one of
# those instead of the real package makes the firmware check below fail on
# a perfectly good ISO.
say "Checking required packages"
FAIL=0
for pkg in switch-confd nic-xl710-i350 kernel; do
    HIT=$(printf '%s' "$LIST" | grep -oE "Packages/${pkg}-[0-9][^ ]*\.rpm" | head -1 || true)
    if [ -n "$HIT" ]; then
        info "OK      ${HIT#Packages/}"
    else
        echo "    MISSING $pkg" >&2; FAIL=1
    fi
done

# --- THE critical check --------------------------------------------------
say "Checking for the Marvell switch firmware"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
SC=$(printf '%s' "$LIST" | grep -oE 'Packages/switch-confd-[0-9][^ ]*\.rpm' | head -1 || true)
[ -n "$SC" ] || die "switch-confd not present - this ISO cannot build an ENCS image"
info "reading ${SC#Packages/}"

bsdtar -xOf "$ISO" "$SC" > "$TMP/sc.rpm" 2>/dev/null
# grep -c, not -q: see the pipefail/SIGPIPE note below.
FW=$(bsdtar -tf "$TMP/sc.rpm" 2>/dev/null | grep -c 'switch_firmware\.bin' || true)
if [ "${FW:-0}" -gt 0 ]; then
    SIZE=$(bsdtar -tvf "$TMP/sc.rpm" 2>/dev/null | grep 'switch_firmware\.bin' | awk '{print $5}')
    info "OK      switch_firmware.bin present (${SIZE} bytes)"
else
    printf '\n\e[31m  FATAL: switch_firmware.bin is NOT in this ISO.\e[0m\n' >&2
    cat >&2 <<'EOF'

  Cisco removed the Marvell switch firmware in NFVIS 4.16 and later. The
  kernel module, loader and platform gate are all still present and the
  install will look completely normal - but the ASIC will never boot, so the
  8 front ports simply will not exist.

  ENCS 5400 support ended at NFVIS 4.15.x. Use a 4.15.x ISO
  (e.g. Cisco_NFVIS-4.15.5-FC4.iso).
EOF
    exit 1
fi

# --- platform gate -------------------------------------------------------
say "Checking the platform whitelist"
# NB: do NOT use `... | grep -q` under `set -o pipefail`. grep -q exits on the
# first match and closes the pipe; the producer then dies of SIGPIPE and
# pipefail marks the whole pipeline failed even though the match succeeded.
# Count instead - grep -c consumes all input.
PLAT=$(strings -a "$TMP/sc.rpm" 2>/dev/null | grep -c 'ENCS5412/K9' || true)
if [ "${PLAT:-0}" -gt 0 ]; then
    info "OK      switch-confd accepts ENCS5406/5408/5412 and CSX-100x"
else
    warn "ENCS5412/K9 not found in the switch-confd scriptlet - platform support may differ"
fi

# --- kernel --------------------------------------------------------------
KVER=$(printf '%s' "$LIST" | grep -oE 'Packages/kernel-([0-9][^ ]*)\.rpm' | head -1 \
       | sed 's|Packages/kernel-||; s|\.rpm$||' || true)
[ -n "$KVER" ] && info "kernel: $KVER  (out-of-tree modules are pinned to exactly this)"

[ "$FAIL" -eq 0 ] || die "required packages are missing"
say "This ISO is usable."
