#!/bin/bash

#############################################################
# Eve X-UI Manager | Prepare Offline Wheels
# Download all Python dependencies for offline installation
# Run this on a machine with internet access
#############################################################

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

print_header() { echo -e "\n${CYAN}${BOLD}  ── $1 ──${NC}\n"; }
print_success() { echo -e "  ${GREEN}✓${NC} $1"; }
print_error() { echo -e "  ${RED}✗${NC} $1"; }
print_warning() { echo -e "  ${YELLOW}⚠${NC} $1"; }

# Get project directory
PROJECT_DIR="${1:-.}"
WHEELS_DIR="$PROJECT_DIR/wheels"
REQUIREMENTS_FILE="$PROJECT_DIR/requirements.txt"

print_header "Eve X-UI Manager: Prepare Offline Wheels"

# Validate inputs
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    print_error "requirements.txt not found in $PROJECT_DIR"
    echo "  Usage: $0 /path/to/eve-xui-manager"
    exit 1
fi

# Create wheels directory
if [ -d "$WHEELS_DIR" ]; then
    print_warning "Wheels directory already exists"
    read -rp "  Overwrite? (y/n) [n]: " _overwrite
    if [[ "$_overwrite" =~ ^[Yy]$ ]]; then
        rm -rf "$WHEELS_DIR"
        print_success "Removed old wheels"
    else
        print_warning "Using existing wheels directory"
    fi
fi

mkdir -p "$WHEELS_DIR"
print_success "Wheels directory: $WHEELS_DIR"

# Check for pip
if ! command -v pip >/dev/null 2>&1; then
    print_error "pip not found. Please install Python and pip first."
    exit 1
fi

PIP_VERSION=$(pip --version)
print_success "Using: $PIP_VERSION"

print_header "Downloading wheels..."

# Download wheels with retry logic
if pip download \
    --no-binary :all: \
    --dest "$WHEELS_DIR" \
    --default-timeout=120 \
    --retries 10 \
    -r "$REQUIREMENTS_FILE"; then
    
    print_success "Downloaded base packages"
else
    print_warning "Some packages failed to download (may be source-only)"
fi

# Also get pre-built wheels as fallback
print_warning "Downloading pre-built wheels (faster installation)..."
if pip wheel \
    --no-build-isolation \
    --wheel-dir "$WHEELS_DIR" \
    --default-timeout=120 \
    --retries 10 \
    -r "$REQUIREMENTS_FILE" 2>/dev/null; then
    
    print_success "Downloaded wheels (pre-built)"
else
    print_warning "Some wheels failed (source-only packages)"
fi

# Count downloaded files
WHEEL_COUNT=$(find "$WHEELS_DIR" -name '*.whl' | wc -l)
TARBALL_COUNT=$(find "$WHEELS_DIR" -name '*.tar.gz' | wc -l)
TOTAL_COUNT=$((WHEEL_COUNT + TARBALL_COUNT))

echo
print_header "Download Summary"
echo -e "  Wheels:    ${WHEEL_COUNT}"
echo -e "  Tarballs:  ${TARBALL_COUNT}"
echo -e "  Total:     ${TOTAL_COUNT}"
echo

if [ $TOTAL_COUNT -lt 10 ]; then
    print_error "Too few packages downloaded. Check your internet connection."
    exit 1
fi

print_success "Wheels prepared successfully!"
echo
echo -e "${BOLD}Next steps:${NC}"
echo "  1. Create a ZIP file with this project:"
echo "     cd $(dirname "$PROJECT_DIR")"
echo "     zip -r eve-xui-manager.zip $(basename "$PROJECT_DIR")/"
echo ""
echo "  2. Upload to server:"
echo "     scp eve-xui-manager.zip root@YOUR_SERVER:/root/"
echo ""
echo "  3. On server, run setup:"
echo "     bash setup.sh"
echo "     Select option [1] Install (choose ZIP source)"
echo ""
echo -e "${YELLOW}Note:${NC} Make sure ${CYAN}wheels/${NC} folder is included in the ZIP!"
echo

# Show largest packages
print_header "Largest packages"
ls -lh "$WHEELS_DIR" | tail -5 | awk '{print "  " $9 " (" $5 ")"}'

print_success "Ready to transfer to offline server!"
