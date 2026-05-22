#!/usr/bin/env bash
# 02_install_ec_axb35_dkms.sh
# Installs the ec_su_axb35 kernel module via DKMS with MOK signing.
# Must be run from the contrib/ubuntu/ directory inside the cloned ec-su_axb35-linux repo.
# Requires 01_setup_dkms_mok.sh to have been run and MOK enrolled at boot.
# Tested under Ubuntu 26.04

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

MOK_KEY="/var/lib/shim-signed/mok/MOK.priv"
MOK_CERT="/var/lib/shim-signed/mok/MOK.der"

[[ $EUID -ne 0 ]] && error "Run as root (sudo $0)"

# Resolve repo root from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && cd .. && pwd)"

# Sanity check we're in the right repo
[[ -f "${REPO_ROOT}/Makefile" && -f "${REPO_ROOT}/Kbuild" ]] \
    || error "Could not find Makefile/Kbuild at ${REPO_ROOT}. Is this script in the repo's scripts/ dir?"

MODULE_NAME="ec_su_axb35"
MODULE_VERSION="1.0"
DKMS_SRC="/usr/src/${MODULE_NAME}-${MODULE_VERSION}"

# Verify MOK keys exist
[[ -f "$MOK_KEY" && -f "$MOK_CERT" ]] \
    || error "MOK keys not found at ${MOK_KEY} / ${MOK_CERT}. Run 01_setup_dkms_mok.sh first."

# Verify MOK is enrolled
if mokutil --sb-state 2>/dev/null | grep -q "SecureBoot enabled"; then
    mokutil --list-enrolled 2>/dev/null | grep "Secure Boot Module Signature key"
      if !mokutil --test-key "$MOK_CERT" 2> /dev/null | grep -q "is already enrolled"; then
        warn "MOK key doesn't appear to be enrolled yet."
        warn "Did you complete the MOK enrollment at the boot screen?"
        read -rp "Continue anyway? [y/N] " confirm
        [[ "$confirm" =~ ^[Yy]$ ]] || exit 0
    else
        info "MOK key confirmed enrolled."
    fi
fi

# Install build deps
info "Installing build dependencies..."
apt-get install -y dkms build-essential "linux-headers-$(uname -r)"

# Remove existing DKMS entry + source if present
if dkms status "${MODULE_NAME}" 2>/dev/null | grep -q "${MODULE_NAME}"; then
    warn "Removing existing DKMS entry for ${MODULE_NAME}..."
    dkms remove "${MODULE_NAME}/${MODULE_VERSION}" --all 2>/dev/null || true
fi
[[ -d "$DKMS_SRC" ]] && rm -rf "$DKMS_SRC"

# Copy repo root into DKMS source tree
info "Copying repo source to ${DKMS_SRC}..."
cp -r "${REPO_ROOT}" "${DKMS_SRC}"

# Write dkms.conf into the DKMS source tree
info "Writing dkms.conf..."
cat > "${DKMS_SRC}/dkms.conf" << EOF
PACKAGE_NAME="${MODULE_NAME}"
PACKAGE_VERSION="${MODULE_VERSION}"
BUILT_MODULE_NAME[0]="${MODULE_NAME}"
DEST_MODULE_LOCATION[0]="/updates"
AUTOINSTALL="yes"
POST_BUILD="sign-file sha256 ${MOK_KEY} ${MOK_CERT} \${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/\${kernelver}/\${arch}/module/\${BUILT_MODULE_NAME[0]}.ko"
EOF

# DKMS add / build / install
info "Adding module to DKMS..."
dkms add "${MODULE_NAME}/${MODULE_VERSION}"

info "Building module..."
dkms build "${MODULE_NAME}/${MODULE_VERSION}"

info "Installing module..."
dkms install "${MODULE_NAME}/${MODULE_VERSION}"

info "DKMS status:"
dkms status "${MODULE_NAME}"

# Load module
info "Loading module..."
modprobe "${MODULE_NAME}"

if dmesg | grep -q "Sixunited AXB35-02 EC driver loaded"; then
    info "Module loaded successfully."
else
    warn "Module loaded but expected dmesg message not found. Check: dmesg | grep -i axb35"
fi

# Auto-load on boot
if grep -q "^${MODULE_NAME}$" /etc/modules 2>/dev/null; then
    info "${MODULE_NAME} already in /etc/modules, skipping."
else
    info "Adding ${MODULE_NAME} to /etc/modules..."
    echo "${MODULE_NAME}" >> /etc/modules
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Done!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Read power mode:  cat /sys/class/ec_su_axb35/apu/power_mode"
echo "Set power mode:   echo performance > /sys/class/ec_su_axb35/apu/power_mode"
echo "                  echo balanced   > /sys/class/ec_su_axb35/apu/power_mode"
echo "                  echo quiet      > /sys/class/ec_su_axb35/apu/power_mode"
echo "Live monitor:     su_axb35_monitor"
echo ""
