#!/bin/bash

#############################################################
# Eve X-UI Manager | Quick Fix for Stuck Installation
# Use this if setup.sh is hanging at Step 7
#############################################################

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

print_header() { echo -e "\n${CYAN}${BOLD}  ── $1 ──${NC}\n"; }
print_success() { echo -e "  ${GREEN}✓${NC} $1"; }
print_error() { echo -e "  ${RED}✗${NC} $1"; }
print_warning() { echo -e "  ${YELLOW}⚠${NC} $1"; }

require_root() {
    if [ "${EUID}" -ne 0 ]; then
        print_error "Run this script as root or with sudo"
        exit 1
    fi
}

require_root

APP_DIR="/opt/eve-xui-manager"
APP_USER="evemgr"

print_header "Eve X-UI Manager: Quick Fix"

# 1. Check if app directory exists
if [ ! -d "$APP_DIR" ]; then
    print_error "App directory not found: $APP_DIR"
    exit 1
fi

print_success "Found app directory: $APP_DIR"

# 2. Check if venv exists
if [ ! -d "$APP_DIR/venv" ]; then
    print_error "Virtual environment not found, creating..."
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
    print_success "Created venv"
else
    print_success "Virtual environment exists"
fi

# 3. Kill any stuck pip processes
print_warning "Killing any stuck pip processes..."
pkill -9 -f 'pip install' 2>/dev/null || true
pkill -9 -f 'pip download' 2>/dev/null || true
sleep 1
print_success "Cleaned up processes"

# 4. Upgrade pip
print_warning "Step 1: Upgrading pip (with extended timeout)..."
sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && \
    pip install --upgrade --default-timeout=120 --retries 10 pip setuptools wheel 2>&1 | tail -5"

print_success "pip upgraded"

# 5. Install requirements
print_warning "Step 2: Installing requirements (main packages)..."

# Check for wheels first
WHEELS_COUNT=0
if [ -d "$APP_DIR/wheels" ]; then
    WHEELS_COUNT=$(find "$APP_DIR/wheels" -type f 2>/dev/null | wc -l)
    print_success "Found $WHEELS_COUNT wheels files"
fi

# Try offline first if wheels available
if [ $WHEELS_COUNT -gt 10 ]; then
    print_warning "Attempting offline installation from wheels..."
    if sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && \
        pip install --no-index --find-links='$APP_DIR/wheels' \
        -r '$APP_DIR/requirements.txt' --default-timeout=120 2>&1 | tail -10"; then
        print_success "Offline installation succeeded!"
    else
        print_warning "Offline failed, trying online..."
    fi
else
    print_warning "No wheels folder or insufficient wheels ($WHEELS_COUNT < 10)"
fi

# Try online with mirrors
print_warning "Step 3: Installing from online mirrors..."

# Try PyPI first
if sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && \
    pip install --default-timeout=120 --retries 10 \
    -r '$APP_DIR/requirements.txt' 2>&1 | tail -10"; then
    print_success "Installation succeeded from PyPI!"
else
    # Try Aliyun mirror
    print_warning "PyPI failed, trying Aliyun mirror..."
    if sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && \
        pip install -i https://mirrors.aliyun.com/pypi/simple/ \
        --default-timeout=120 --retries 10 \
        -r '$APP_DIR/requirements.txt' 2>&1 | tail -10"; then
        print_success "Installation succeeded from Aliyun mirror!"
    else
        # Try Tsinghua mirror
        print_warning "Aliyun failed, trying Tsinghua mirror..."
        if sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && \
            pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
            --default-timeout=120 --retries 10 \
            -r '$APP_DIR/requirements.txt' 2>&1 | tail -10"; then
            print_success "Installation succeeded from Tsinghua mirror!"
        else
            print_error "All installation methods failed"
            echo ""
            echo -e "${YELLOW}What to try next:${NC}"
            echo "  1. Check disk space:"
            echo "     df -h $APP_DIR"
            echo ""
            echo "  2. Check pip manually:"
            echo "     source $APP_DIR/venv/bin/activate"
            echo "     pip install requests==2.32.5 --default-timeout=120 --retries 10"
            echo ""
            echo "  3. Use diagnose script:"
            echo "     bash $APP_DIR/diagnose.sh"
            echo ""
            echo "  4. Get wheel files and retry:"
            echo "     bash $APP_DIR/prepare-wheels.sh $APP_DIR"
            echo ""
            exit 1
        fi
    fi
fi

# 6. Install gunicorn and psycopg2
print_warning "Step 4: Installing gunicorn and psycopg2..."
sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && \
    pip install --default-timeout=120 --retries 10 gunicorn psycopg2-binary 2>&1 | tail -5" || {
    print_warning "gunicorn/psycopg2 install warning (may be ok)"
}

# 7. Verify installation
print_warning "Step 5: Verifying installation..."
PACKAGE_COUNT=$(sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && pip list 2>/dev/null | tail -n +3" | wc -l)
print_success "Total installed packages: $PACKAGE_COUNT"

# Check critical packages
echo ""
print_warning "Checking critical packages:"
CRITICAL=("flask" "flask-limiter" "flask-sqlalchemy" "gunicorn" "requests")
for pkg in "${CRITICAL[@]}"; do
    if sudo -u "$APP_USER" bash -c "source $APP_DIR/venv/bin/activate && pip list 2>/dev/null | grep -q '^$pkg '" 2>/dev/null; then
        print_success "$pkg"
    else
        print_error "$pkg MISSING!"
    fi
done

# 8. Restart service
print_warning "Step 6: Restarting service..."
systemctl restart eve-manager || print_warning "Service restart warning"

sleep 2
if systemctl is-active --quiet eve-manager; then
    print_success "Service is running!"
else
    print_warning "Service failed to start"
    echo "  Check logs: journalctl -u eve-manager -f -n 20"
fi

echo ""
print_header "✓ Fix Complete!"
echo ""
echo "  Service: $(systemctl is-active eve-manager)"
echo "  Logs: journalctl -u eve-manager -f"
echo "  Check: bash $APP_DIR/diagnose.sh"
echo ""
