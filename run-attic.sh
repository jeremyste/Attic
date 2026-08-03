#!/usr/bin/env bash
# One-click launcher for Attic.
#
# Checks everything the app needs before starting it, and offers to install
# what is missing rather than failing with a traceback:
#
#   * python3 + venv support
#   * the project virtualenv and its Python packages (PyQt6, ...)
#   * the external CLI tools the pipelines shell out to
#   * membership of the 'dialout' group, needed to talk to the Greaseweazle
#
# Safe to re-run; it only acts on what is actually missing. Run it from
# anywhere -- it resolves the project directory from its own location.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="$HERE/.venv"
PY="$VENV/bin/python"

# --- output helpers ---------------------------------------------------------

if [ -t 1 ]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; GREEN=$'\033[32m'; OFF=$'\033[0m'
else
    BOLD=''; RED=''; YELLOW=''; GREEN=''; OFF=''
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s[ok]%s   %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$YELLOW" "$OFF" "$*"; }
bad()  { printf '  %s[!!]%s   %s\n' "$RED" "$OFF" "$*"; }
die()  { printf '\n%sCannot start:%s %s\n' "$RED" "$OFF" "$*" >&2; hold; exit 1; }

# Keep the window open when launched by double-click from a file manager.
hold() {
    if [ -t 0 ] && [ -n "${ATTIC_LAUNCHED_FROM_GUI:-}" ]; then
        printf '\nPress Enter to close...'
        read -r _ || true
    fi
}

ask() {
    # ask "question" -> 0 for yes. Defaults to no when there is no terminal.
    local reply
    if [ ! -t 0 ]; then
        return 1
    fi
    printf '%s [y/N] ' "$1"
    read -r reply || return 1
    [[ "$reply" =~ ^[Yy] ]]
}

# --- detect the system package manager --------------------------------------

PKG_INSTALL=""
case "$(. /etc/os-release 2>/dev/null && echo "${ID_LIKE:-$ID}")" in
    *debian*|*ubuntu*) PKG_INSTALL="sudo apt install -y" ;;
    *fedora*|*rhel*)   PKG_INSTALL="sudo dnf install -y" ;;
    *arch*)            PKG_INSTALL="sudo pacman -S --needed" ;;
    *suse*)            PKG_INSTALL="sudo zypper install -y" ;;
esac

# Package names differ per distro; only Debian/Ubuntu names are spelled out.
pkg_for() {
    case "$1" in
        ddrescue)  echo "gddrescue" ;;
        mdir|mcopy|mlabel) echo "mtools" ;;
        7z)        echo "p7zip-full" ;;
        blkid)     echo "util-linux" ;;
        *)         echo "$1" ;;
    esac
}

say ""
say "${BOLD}Attic${OFF} - checking the environment"
say ""

# --- 1. python3 -------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    die "python3 is not installed. Install it with your package manager, e.g.
      ${PKG_INSTALL:-sudo apt install -y} python3 python3-venv"
fi
ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    die "Python 3.10 or newer is required (found $(python3 -V 2>&1))."
fi

# --- 2. virtualenv ----------------------------------------------------------

if [ ! -x "$PY" ]; then
    warn "no virtualenv at .venv"
    if ! python3 -c 'import venv' 2>/dev/null; then
        die "the 'venv' module is missing. Install it with:
      ${PKG_INSTALL:-sudo apt install -y} python3-venv"
    fi
    if ask "  Create the virtualenv now?"; then
        python3 -m venv "$VENV" || die "could not create the virtualenv."
        ok "created .venv"
    else
        die "a virtualenv is required. Re-run and accept, or: python3 -m venv .venv"
    fi
else
    ok "virtualenv"
fi

# --- 3. python packages -----------------------------------------------------

missing_py=""
"$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null || missing_py="yes"
if [ -n "$missing_py" ]; then
    warn "Python dependencies are not installed"
    if ask "  Install them into .venv now (pip install -r requirements.txt)?"; then
        "$PY" -m pip install --upgrade pip >/dev/null
        "$PY" -m pip install -r "$HERE/requirements.txt" \
            || die "pip install failed. See the output above."
        ok "installed Python dependencies"
    else
        die "PyQt6 is required. Install with:
      $PY -m pip install -r requirements.txt"
    fi
else
    ok "Python dependencies"
fi

# --- 4. external CLI tools --------------------------------------------------

# Hard requirements: the app cannot function without these.
REQUIRED=(zstd sha256sum)
# Per-pipeline; a missing one only disables that pipeline, so just warn.
declare -A OPTIONAL=(
    [gw]="floppy capture (Greaseweazle)"
    [ddrescue]="HDD and optical imaging"
    [blkid]="filesystem detection"
    [mdir]="FAT extraction (mtools)"
    [7z]="archive extraction fallback"
)

missing_req=()
for tool in "${REQUIRED[@]}"; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool"
    else
        bad "$tool is missing"
        missing_req+=("$(pkg_for "$tool")")
    fi
done

# gw may live only inside the venv, which is fine -- the app inherits PATH.
if [ -x "$VENV/bin/gw" ]; then
    export PATH="$VENV/bin:$PATH"
fi

missing_opt=()
for tool in "${!OPTIONAL[@]}"; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool (${OPTIONAL[$tool]})"
    else
        warn "$tool missing - ${OPTIONAL[$tool]} will not work"
        missing_opt+=("$(pkg_for "$tool")")
    fi
done

install_pkgs() {
    local -n list=$1
    [ ${#list[@]} -eq 0 ] && return 0
    local uniq
    mapfile -t uniq < <(printf '%s\n' "${list[@]}" | sort -u)
    if [ -z "$PKG_INSTALL" ]; then
        say "  Install these with your package manager: ${uniq[*]}"
        return 1
    fi
    say "  Would run: $PKG_INSTALL ${uniq[*]}"
    if ask "  Install now?"; then
        $PKG_INSTALL "${uniq[@]}" || return 1
    else
        return 1
    fi
}

if [ ${#missing_req[@]} -gt 0 ]; then
    say ""
    install_pkgs missing_req || die "required tools are missing: ${missing_req[*]}"
fi

if [ ${#missing_opt[@]} -gt 0 ]; then
    say ""
    say "  Some pipelines are unavailable without the tools above."
    install_pkgs missing_opt || true
fi

# --- 5. Greaseweazle device access ------------------------------------------

if command -v gw >/dev/null 2>&1; then
    # Pure-bash membership test: no dependency on grep, which keeps this check
    # honest even on a minimal PATH (a missing grep must not fake a warning).
    if compgen -G "/dev/ttyACM*" >/dev/null && [[ " $(id -nG) " != *" dialout "* ]]; then
        warn "you are not in the 'dialout' group; the Greaseweazle may be unreadable"
        say  "        fix with:  sudo usermod -aG dialout $USER   (then log out and back in)"
    fi
fi

# --- launch -----------------------------------------------------------------

say ""
say "${BOLD}Starting Attic...${OFF}"
say ""
exec "$PY" "$HERE/main.py" "$@"
