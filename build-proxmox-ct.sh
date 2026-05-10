#!/usr/bin/env bash
# shellcheck shell=bash
# =============================================================================
# build-proxmox-ct.sh  –  Build a Proxmox LXC rootfs from WSL2 (no lxc-start)
#
# Run this from your project root (where rscli/, server.py, etc. live).
#
# Usage:
#   sudo bash build-proxmox-ct.sh [TARGETARCH] [OUTPUT_NAME]
#
# Examples:
#   sudo bash build-proxmox-ct.sh amd64 slopsmith-ct
#   sudo bash build-proxmox-ct.sh arm64 slopsmith-ct
#
# Environment variables:
#   ROCKSMITH_SRC_DLC   Path to Rocksmith2014 install (default: /mnt/z/Steam/...)
#   SKIP_HASH_CHECK=1   Bypass SHA256 verification — for unpinned hashes OR
#                       to override mismatches when an upstream artifact rolls
#                       (e.g. dot.net/v1/dotnet-install.sh). Use with caution.
#   KEEP_BUILD_DIR=1    Retain ${BUILD_BASE} after a successful build
#   FORCE_REBUILD=1     Delete an existing rootfs without prompting (for CI)
#
# Prerequisites (install in WSL):
#   sudo apt install debootstrap systemd-container tar zstd curl unzip git
#
# On Proxmox, after transfer:
#   pct restore <VMID> slopsmith-ct.tar.zst --storage local-lvm --rootfs 8 --unprivileged 1
# =============================================================================

set -euo pipefail

TARGETARCH="${1:-amd64}"
OUTPUT_NAME="${2:-slopsmith-ct}"

# debootstrap requires a real Linux filesystem (ext4/tmpfs) – it creates
# device nodes that NTFS/FUSE mounts (/mnt/c, /mnt/d …) cannot represent.
# We build everything under /tmp (tmpfs) and copy the final tarball back.
PROJECT_DIR="$(pwd)"          # may be on /mnt/d – that's fine for source files
BUILD_BASE="/tmp/proxmox-ct-build"
ROOTFS="${BUILD_BASE}/rootfs"
mkdir -p "$BUILD_BASE"

DOTNET_CHANNEL="10.0"
VGMSTREAM_URL="https://github.com/vgmstream/vgmstream/releases/download/r2083/vgmstream-linux-cli.zip"
# Supply-chain hashes — regenerate with:
#   curl -fsSL <URL> | sha256sum
# Set SKIP_HASH_CHECK=1 to bypass verification (e.g. when Microsoft rolls
# dotnet-install.sh and the pinned hash hasn't been refreshed yet).
VGMSTREAM_SHA256="7fc17225b7a49b8f1e7850f6cc5bdaf73c35e81ee5774bb12211ebc85188a4ff"
# dot.net/v1/dotnet-install.sh is a rolling URL; refresh this hash whenever
# Microsoft updates the installer (the build will abort with a clear mismatch).
DOTNET_INSTALL_SHA256="102a6849303713f15462bb28eb10593bf874bbeec17122e0522f10a3b57ce442"

APP_DIR="/app"
VENV_DIR="/opt/app-venv"
RSCLI_DIR="/opt/rscli"
DLC_DIR="/dlc"
CONFIG_DIR="/config"
ROCKSMITH_DIR="/rocksmith"
ROCKSMITH_SRC_DLC="${ROCKSMITH_SRC_DLC:-/mnt/z/Steam/steamapps/common/Rocksmith2014}"
SVC_USER="slopsmith"

# Coloured logging
info() { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()  { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

cleanup() {
  local rc=$?
  if [[ $rc -ne 0 && -d "${BUILD_BASE:-}" ]]; then
    warn "Build failed (exit $rc). Partial rootfs left at ${BUILD_BASE} for inspection."
    warn "Run 'sudo rm -rf ${BUILD_BASE}' to clean up."
  elif [[ $rc -eq 0 && -d "${BUILD_BASE:-}" && "${KEEP_BUILD_DIR:-0}" != "1" ]]; then
    info "Removing build directory ${BUILD_BASE} (set KEEP_BUILD_DIR=1 to retain)."
    rm -rf "${BUILD_BASE}"
  fi
}
trap cleanup EXIT

# Verify a downloaded file against a pinned SHA256 hash.
# Skips verification when the expected hash is empty (not yet pinned).
verify_sha256() {
  local file="$1" expected="$2" label="${3:-$1}"
  if [[ -z "$expected" ]]; then
    if [[ "${SKIP_HASH_CHECK:-0}" != "1" ]]; then
      die "No SHA256 pinned for ${label}. Pin the hash or set SKIP_HASH_CHECK=1 to proceed."
    fi
    warn "No SHA256 pinned for ${label} — skipping verification (SKIP_HASH_CHECK=1)."
    return 0
  fi
  local actual
  actual=$(sha256sum "$file" | awk '{print $1}')
  if [[ "$actual" != "$expected" ]]; then
    if [[ "${SKIP_HASH_CHECK:-0}" == "1" ]]; then
      warn "SHA256 mismatch for ${label} (expected ${expected}, got ${actual}) — continuing because SKIP_HASH_CHECK=1."
      return 0
    fi
    die "SHA256 mismatch for ${label}:\n" \
        "       expected: ${expected}\n" \
        "       got:      ${actual}\n" \
        "       Refresh the pinned hash, or set SKIP_HASH_CHECK=1 to bypass."
  fi
  ok "SHA256 verified for ${label}."
}

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

case "$TARGETARCH" in
  arm64) RID="linux-arm64" ; DEBIAN_ARCH="arm64" ;;
  amd64) RID="linux-x64"   ; DEBIAN_ARCH="amd64" ;;
  *)     die "Unsupported TARGETARCH: ${TARGETARCH}. Expected: amd64 | arm64" ;;
esac

# arm64 cross-builds require qemu-user-static + a registered binfmt handler.
# Checking the binary alone isn't enough — without a registered+enabled handler
# debootstrap/nspawn fail later with "exec format error" after significant
# wasted setup time.
if [[ "$TARGETARCH" == "arm64" && "$(uname -m)" != "aarch64" ]]; then
  if ! command -v qemu-aarch64-static &>/dev/null; then
    die "arm64 builds on a non-arm64 host require qemu-user-static.\n" \
        "       Install with: sudo apt install qemu-user-static binfmt-support\n" \
        "       Then re-run this script."
  fi
  binfmt_reg=""
  for f in /proc/sys/fs/binfmt_misc/qemu-aarch64 \
           /proc/sys/fs/binfmt_misc/qemu-aarch64-static; do
    [[ -f "$f" ]] && grep -q '^enabled' "$f" 2>/dev/null && { binfmt_reg="$f"; break; }
  done
  if [[ -z "$binfmt_reg" ]]; then
    die "arm64 binfmt handler not registered or not enabled.\n" \
        "       Register with: sudo apt install qemu-user-static binfmt-support\n" \
        "       Or:           docker run --rm --privileged multiarch/qemu-user-static --reset -p yes\n" \
        "       Then verify:  grep ^enabled /proc/sys/fs/binfmt_misc/qemu-aarch64*"
  fi
fi

# Confirm required tools
for cmd in debootstrap systemd-nspawn curl unzip git tar zstd; do
  command -v "$cmd" &>/dev/null || die "'$cmd' not found. Run: sudo apt install debootstrap systemd-container curl unzip git tar zstd"
done

# =============================================================================
# Helper: run a command inside the rootfs via systemd-nspawn
# --pipe keeps stdin/stdout connected; 
# Or --quiet suppresses nspawn chatter.
# =============================================================================
r() {
  systemd-nspawn \
    --quiet \
    --directory="$ROOTFS" \
    --bind-ro=/etc/resolv.conf:/etc/resolv.conf \
    -- bash -c "set -e; $1"
}

# =============================================================================
# 1. Bootstrap a minimal Debian Trixie rootfs
# =============================================================================
info "Bootstrapping Debian Trixie (${DEBIAN_ARCH}) rootfs at ${ROOTFS} …"
if [[ -d "$ROOTFS" ]]; then
  if [[ "${FORCE_REBUILD:-0}" == "1" ]]; then
    info "FORCE_REBUILD=1 — removing existing rootfs at ${ROOTFS}."
    rm -rf "$ROOTFS" || die "Failed to remove existing rootfs at ${ROOTFS}."
  elif [[ -t 0 ]]; then
    warn "Existing rootfs found at ${ROOTFS} – remove it to rebuild from scratch."
    read -rp "    Delete and rebuild? [y/N] " yn
    if [[ "$yn" =~ ^[Yy]$ ]]; then
      rm -rf "$ROOTFS" || die "Failed to remove existing rootfs at ${ROOTFS}."
    else
      die "Aborting."
    fi
  else
    die "Existing rootfs at ${ROOTFS}; rerun with FORCE_REBUILD=1 to overwrite."
  fi
fi

debootstrap \
  --arch="$DEBIAN_ARCH" \
  --include=ca-certificates,curl,gnupg \
  trixie \
  "$ROOTFS" \
  https://deb.debian.org/debian

ok "Bootstrap complete."

# DNS during the build is supplied by the host's /etc/resolv.conf, which
# r() bind-mounts read-only into nspawn. The rootfs's own /etc/resolv.conf
# gets replaced with a systemd-resolved stub symlink in step 10(d).

# =============================================================================
# 2. System packages  (mirrors Stage 2 apt block)
# =============================================================================
info "Installing system packages …"
r "apt-get update -qq && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    ffmpeg \
    fluidsynth \
    fluid-soundfont-gm \
    libsndfile1 \
    curl \
    unzip \
    megatools \
    git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*"
ok "System packages installed."

# =============================================================================
# 3. Install .NET  (needed to build AND run RsCli – no SDK in final Docker
#    stage, but in an LXC the runtime must be present)
# =============================================================================
info "Installing .NET ${DOTNET_CHANNEL} runtime + SDK …"
r "curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh"
verify_sha256 "${ROOTFS}/tmp/dotnet-install.sh" "${DOTNET_INSTALL_SHA256}" "dotnet-install.sh"
r "chmod +x /tmp/dotnet-install.sh \
    && DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
       DOTNET_CLI_TELEMETRY_OPTOUT=1 \
       /tmp/dotnet-install.sh --channel ${DOTNET_CHANNEL} \
           --install-dir /usr/share/dotnet \
    && ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
    && rm /tmp/dotnet-install.sh"
ok ".NET installed."

# =============================================================================
# 4. Build RsCli inside the rootfs
# =============================================================================
info "Cloning Rocksmith2014.NET into rootfs (host-side) …"
rm -rf "${ROOTFS}/opt/rs2014"
git clone --depth 1 https://github.com/iminashi/Rocksmith2014.NET.git "${ROOTFS}/opt/rs2014"
 
info "Copying rscli sources …"
[[ -f "${PROJECT_DIR}/rscli/RsCli.fsproj" ]] || die "rscli/RsCli.fsproj not found."
[[ -f "${PROJECT_DIR}/rscli/Program.fs"   ]] || die "rscli/Program.fs not found."
mkdir -p "${ROOTFS}/opt/rs2014/tools/RsCli"
cp "${PROJECT_DIR}/rscli/RsCli.fsproj" "${ROOTFS}/opt/rs2014/tools/RsCli/"
cp "${PROJECT_DIR}/rscli/Program.fs"   "${ROOTFS}/opt/rs2014/tools/RsCli/"
 
# NuGetAudit=false: Rocksmith2014.NET pins older NuGet dependencies that
# trigger audit warnings.  We don't ship the SDK in the final image — only
# the self-contained publish output — so these warnings are noise during a
# build-time-only step.  Re-enable if you upgrade the upstream project.
info "Patching Directory.Build.props (host-side) …"
PROPS=$(find "${ROOTFS}/opt/rs2014" -name "Directory.Build.props" | head -1)
if [[ -z "$PROPS" ]]; then
  warn "Directory.Build.props not found – skipping NuGetAudit patch"
else
  info "  Patching: ${PROPS#$ROOTFS}"
  sed -i 's|</PropertyGroup>|<NuGetAudit>false</NuGetAudit></PropertyGroup>|' "$PROPS"
fi
 
# Compute the path as seen inside the container (strip the host rootfs prefix)
FSPROJ_HOST=$(find "${ROOTFS}/opt/rs2014/tools/RsCli" -name "*.fsproj" 2>/dev/null | head -1)
[[ -n "$FSPROJ_HOST" ]] || die "RsCli.fsproj not found under ${ROOTFS}/opt/rs2014/tools/RsCli"
FSPROJ_INNER="${FSPROJ_HOST#$ROOTFS}"
FSPROJ_DIR_INNER="$(dirname "$FSPROJ_INNER")"
info "  Building project at (container path): ${FSPROJ_DIR_INNER}"
 
r "export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    && export DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    && cd '${FSPROJ_DIR_INNER}' \
    && dotnet publish -c Release -r '${RID}' --self-contained -o '${RSCLI_DIR}'"
 
# Clean up build artifacts to keep the image lean
rm -rf "${ROOTFS}/opt/rs2014" "${ROOTFS}/root/.nuget" "${ROOTFS}/root/.dotnet/toolResolverCache"
ok "RsCli built → ${RSCLI_DIR}"
 
# Tip: remove the SDK after build to save ~300 MB (runtime stays):
rm -rf "${ROOTFS}/usr/share/dotnet/sdk"
 
# =============================================================================
# 5. vgmstream-cli
# =============================================================================
info "Installing vgmstream-cli …"
r "curl -fSL '${VGMSTREAM_URL}' -o /tmp/vgm.zip"
verify_sha256 "${ROOTFS}/tmp/vgm.zip" "${VGMSTREAM_SHA256}" "vgmstream-linux-cli.zip"
r "unzip -o /tmp/vgm.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/vgmstream-cli \
    && rm /tmp/vgm.zip"
ok "vgmstream-cli installed."

# =============================================================================
# 6. Python application
# =============================================================================
info "Setting up Python application …"
mkdir -p \
  "${ROOTFS}${APP_DIR}/lib" \
  "${ROOTFS}${APP_DIR}/static" \
  "${ROOTFS}${APP_DIR}/plugins"

for d in lib static plugins; do
  if [[ -d "$d" ]]; then
    cp -r "${d}/." "${ROOTFS}${APP_DIR}/${d}/"
    info "  Copied ${d}/"
  else
    warn "  Local '${d}/' not found – skipping."
  fi
done

for f in requirements.txt server.py VERSION main.py; do
  if [[ -f "$f" ]]; then
    cp "$f" "${ROOTFS}${APP_DIR}/"
    info "  Copied ${f}"
  else
    if [[ "$f" == "requirements.txt" || "$f" == "main.py" ]]; then
      die "  '${f}' not found — required for the service to start."
    fi
    warn "  '${f}' not found – skipping."
  fi
done

info "Creating Python venv and installing dependencies …"
r "python3 -m venv ${VENV_DIR} \
    && ${VENV_DIR}/bin/pip install --no-cache-dir -r ${APP_DIR}/requirements.txt"
ok "Python venv + dependencies installed."

# =============================================================================
# 7. Data directories + assets
# =============================================================================
info "Populating data directories …"
mkdir -p "${ROOTFS}${CONFIG_DIR}" "${ROOTFS}${DLC_DIR}" "${ROOTFS}${ROCKSMITH_DIR}"

if [[ -d "config" ]]; then
    cp -r config/. "${ROOTFS}${CONFIG_DIR}/"
    info "  Copied config/"
  else
    warn "  config/ not found."
  fi

if compgen -G "${ROCKSMITH_SRC_DLC}/dlc/*_p.psarc" &>/dev/null; then
  cp "${ROCKSMITH_SRC_DLC}"/dlc/*_p.psarc "${ROOTFS}${DLC_DIR}/"
  info "  Copied DLC psarc files."
else
  warn "  No *_p.psarc files found – copy them into ${DLC_DIR} on Proxmox."
fi

if [[ -f "${ROCKSMITH_SRC_DLC}/songs.psarc" ]]; then
    cp "${ROCKSMITH_SRC_DLC}/songs.psarc" "${ROOTFS}${ROCKSMITH_DIR}/"
    info "  Copied songs.psarc"
  else
    warn "  songs.psarc not found."
  fi

# =============================================================================
# 8. Environment variables
# =============================================================================
info "Writing /etc/environment …"
cat > "${ROOTFS}/etc/environment" <<EOF
PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PYTHONPATH=${APP_DIR}/lib:${APP_DIR}
RSCLI_PATH=${RSCLI_DIR}/RsCli
DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
DLC_DIR=${DLC_DIR}
CONFIG_DIR=${CONFIG_DIR}
EOF

# =============================================================================
# 9. systemd service for uvicorn
# =============================================================================
info "Creating service user '${SVC_USER}' …"
r "useradd --system --home-dir ${APP_DIR} --shell /usr/sbin/nologin ${SVC_USER}"
ok "User '${SVC_USER}' created."

info "Installing slopsmith-server.service …"
mkdir -p "${ROOTFS}/etc/systemd/system"
cat > "${ROOTFS}/etc/systemd/system/slopsmith-server.service" <<EOF
[Unit]
Description=Slopsmith uvicorn server
After=network.target

[Service]
User=${SVC_USER}
# Default port (8000) is non-privileged; uncomment the next line only if
# you set PORT<1024 in /etc/environment so the unit can bind it.
# AmbientCapabilities=CAP_NET_BIND_SERVICE
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/environment
ExecStart=${VENV_DIR}/bin/python3 main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Enable by symlinking (avoids running systemctl inside nspawn)
mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
ln -sf /etc/systemd/system/slopsmith-server.service \
       "${ROOTFS}/etc/systemd/system/multi-user.target.wants/slopsmith-server.service"
ok "Service enabled."

# =============================================================================
# 10. Proxmox-specific tweaks
# =============================================================================
info "Applying Proxmox CT compatibility tweaks …"

# (a) Ensure a working /etc/hostname and /etc/hosts
echo "slopsmith" > "${ROOTFS}/etc/hostname"
cat > "${ROOTFS}/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   slopsmith
::1         localhost ip6-localhost ip6-loopback
EOF

# (b) Clear machine-id so Proxmox generates a fresh one on first boot
# A pre-filled machine-id can cause network/systemd conflicts across clones.
echo -n > "${ROOTFS}/etc/machine-id"
[[ -f "${ROOTFS}/var/lib/dbus/machine-id" ]] && echo -n > "${ROOTFS}/var/lib/dbus/machine-id"

# (c) DHCP networking via systemd-networkd (Proxmox expects this for unprivileged CTs)
mkdir -p "${ROOTFS}/etc/systemd/network"
cat > "${ROOTFS}/etc/systemd/network/20-eth0.network" <<EOF
[Match]
Name=eth0

[Network]
DHCP=yes
EOF

# Enable via symlinks on the host – systemctl inside nspawn needs a running
# init which WSL doesn't provide.

for svc in systemd-networkd systemd-resolved; do
  mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
  ln -sf "/lib/systemd/system/${svc}.service"          "${ROOTFS}/etc/systemd/system/multi-user.target.wants/${svc}.service" 2>/dev/null || true
done

# (d) Fix resolv.conf to use systemd-resolved stub
rm -f "${ROOTFS}/etc/resolv.conf"
ln -sf /run/systemd/resolve/stub-resolv.conf "${ROOTFS}/etc/resolv.conf"

# (e) Ensure correct permissions on key dirs
SVC_UID="$(r "id -u ${SVC_USER}")"
SVC_GID="$(r "id -g ${SVC_USER}")"
chown -R "${SVC_UID}:${SVC_GID}" \
              "${ROOTFS}${APP_DIR}" "${ROOTFS}${CONFIG_DIR}" \
              "${ROOTFS}${DLC_DIR}" "${ROOTFS}${VENV_DIR}"
chown -R 0:0 "${ROOTFS}${RSCLI_DIR}" \
              "${ROOTFS}${ROCKSMITH_DIR}"

ok "Proxmox tweaks applied."

# =============================================================================
# 11. Package as a Proxmox-importable .tar.zst
# =============================================================================
OUTPUT_FILE="${OUTPUT_NAME}.tar.zst"
info "Creating ${OUTPUT_FILE} …"

# Proxmox pct restore expects a plain rootfs tarball (no ./rootfs/ prefix).
tar \
  --numeric-owner \
  --xattrs \
  --acls \
  -C "$ROOTFS" \
  -c . \
  | zstd -T0 -9 > "$OUTPUT_FILE"

ok "Template ready: $(pwd)/${OUTPUT_FILE}  ($(du -sh "$OUTPUT_FILE" | cut -f1))"

# =============================================================================
# Done
# =============================================================================
cat <<DONE

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Build complete!

  Transfer to Proxmox:
    scp ${OUTPUT_FILE} root@<proxmox-host>:/var/lib/vz/template/cache/

  Import on Proxmox (pick an unused VMID, e.g. 200):
    pct restore 200 /var/lib/vz/template/cache/${OUTPUT_FILE} \\
        --storage local-lvm \\
        --rootfs 8 \\
        --memory 2048 \\
        --cores 2 \\
        --net0 name=eth0,bridge=vmbr0,ip=dhcp \\
        --unprivileged 1 \\
        --start 1

  Then check the server:
    pct exec 200 -- systemctl status slopsmith-server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DONE
