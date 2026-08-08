#!/bin/bash
# Shared helpers for the ENCS 5412 build scripts.

say()  { printf '\n\e[36m==> %s\e[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\e[33m    WARNING: %s\e[0m\n' "$*" >&2; }
die()  { printf '\e[31mERROR: %s\e[0m\n' "$*" >&2; exit 1; }

# Everything the build needs, and the Debian/Ubuntu package that provides it.
REQUIRED_TOOLS=(
    "bsdtar:libarchive-tools"
    "xorriso:xorriso"
    "createrepo_c:createrepo-c"
    "modifyrepo_c:createrepo-c"
    "qemu-system-x86_64:qemu-system-x86"
    "qemu-img:qemu-utils"
    "python3:python3"
)

check_deps() {
    local missing=() pkgs=()
    for entry in "${REQUIRED_TOOLS[@]}"; do
        local tool="${entry%%:*}" pkg="${entry##*:}"
        command -v "$tool" >/dev/null 2>&1 || { missing+=("$tool"); pkgs+=("$pkg"); }
    done
    # OVMF is a file, not a command.
    local ovmf
    ovmf=$(ls /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd 2>/dev/null | head -1 || true)
    [ -n "$ovmf" ] || { missing+=("OVMF firmware"); pkgs+=("ovmf"); }

    if [ ${#missing[@]} -gt 0 ]; then
        echo "Missing build dependencies: ${missing[*]}" >&2
        echo >&2
        # shellcheck disable=SC2207
        local uniq=($(printf '%s\n' "${pkgs[@]}" | sort -u))
        echo "  Debian/Ubuntu:  sudo apt-get install -y ${uniq[*]}" >&2
        echo "  Fedora/RHEL:    sudo dnf install -y libarchive xorriso createrepo_c qemu-kvm edk2-ovmf" >&2
        echo >&2
        return 1
    fi
    return 0
}

check_kvm() {
    if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
        echo kvm
    else
        warn "no access to /dev/kvm - the qcow2 build will use software emulation (very slow)."
        warn "fix with:  sudo usermod -aG kvm \$USER   (then log out and back in)"
        echo tcg
    fi
}

# Locate OVMF firmware, preferring the 4MB build.
ovmf_code() { ls /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd 2>/dev/null | head -1 || true; }
ovmf_vars() { ls /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd 2>/dev/null | head -1 || true; }
