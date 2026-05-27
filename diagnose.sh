#!/bin/bash

#############################################################
# Eve X-UI Manager | Diagnostic Tool
# Check installation status and troubleshoot pip issues
#############################################################

set -euo pipefail

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

APP_DIR="${1:-/opt/eve-xui-manager}"

print_header "Eve X-UI Manager: Diagnostic Check"

# 1. Check directory
if [ ! -d "$APP_DIR" ]; then
    print_error "App directory not found: $APP_DIR"
    exit 1
fi
print_success "App directory found: $APP_DIR"

# 2. Check venv
if [ ! -d "$APP_DIR/venv" ]; then
    print_error "Virtual environment not found"
    exit 1
fi
print_success "Virtual environment exists"

# 3. Check Python version
PYTHON_VERSION=$(sudo -u evemgr bash -c "source $APP_DIR/venv/bin/activate && python --version 2>&1" || echo "Unknown")
print_success "Python version: $PYTHON_VERSION"

# 4. Check pip
PIP_VERSION=$(sudo -u evemgr bash -c "source $APP_DIR/venv/bin/activate && pip --version 2>&1" || echo "Failed")
print_success "Pip status: $PIP_VERSION"

# 5. Check wheels folder
print_header "Wheels Folder"
if [ -d "$APP_DIR/wheels" ]; then
    WHEELS_COUNT=$(find "$APP_DIR/wheels" -type f 2>/dev/null | wc -l)
    print_success "Wheels folder exists with $WHEELS_COUNT files"
    ls -lh "$APP_DIR/wheels" | head -10
else
    print_warning "Wheels folder not found (offline mode unavailable)"
fi

# 6. Check requirements.txt
print_header "Dependencies"
if [ ! -f "$APP_DIR/requirements.txt" ]; then
    print_error "requirements.txt not found"
    exit 1
fi

REQUIRED_PACKAGES=$(cat "$APP_DIR/requirements.txt" | grep -v '^#' | grep -v '^$' | wc -l)
print_success "Found $REQUIRED_PACKAGES required packages"
echo "  Packages in requirements.txt:"
cat "$APP_DIR/requirements.txt" | grep -v '^#' | grep -v '^$' | sed 's/^/    /'

# 7. Check installed packages
print_header "Installed Packages"
INSTALLED=$(sudo -u evemgr bash -c "source $APP_DIR/venv/bin/activate && pip list 2>/dev/null | tail -n +3" | wc -l)
print_success "Total installed packages: $INSTALLED"

# Check critical packages
CRITICAL_PACKAGES=("flask" "flask-limiter" "flask-sqlalchemy" "gunicorn" "requests")
for pkg in "${CRITICAL_PACKAGES[@]}"; do
    if sudo -u evemgr bash -c "source $APP_DIR/venv/bin/activate && pip list 2>/dev/null | grep -q '^$pkg '" 2>/dev/null; then
        print_success "$pkg is installed"
    else
        print_warning "$pkg is NOT installed"
    fi
done

# 8. Test pip connectivity
print_header "Network Test"
print_warning "Testing pip connectivity (this may take a moment)..."

if timeout 10 sudo -u evemgr bash -c "source $APP_DIR/venv/bin/activate && pip index versions requests 2>&1 | head -3" >/dev/null 2>&1; then
    print_success "pip can reach PyPI"
else
    print_warning "pip cannot reach PyPI (expected in Iran)"
    print_warning "Testing Aliyun mirror..."
    if timeout 10 sudo -u evemgr bash -c "source $APP_DIR/venv/bin/activate && pip install --dry-run -i https://mirrors.aliyun.com/pypi/simple/ requests 2>&1" >/dev/null 2>&1; then
        print_success "Aliyun mirror is accessible!"
    else
        print_error "Aliyun mirror also not accessible"
    fi
fi

# 9. Check .env
print_header "Configuration"
if [ -f "$APP_DIR/.env" ]; then
    print_success ".env file exists"
    echo "  Environment variables:"
    sudo -u evemgr grep -E '^[A-Z_]+=' "$APP_DIR/.env" | sed 's/=.*/=***/' | sed 's/^/    /'
else
    print_warning ".env file not found"
fi

# 10. Check service
print_header "Service Status"
if systemctl is-active --quiet eve-manager 2>/dev/null; then
    print_success "eve-manager service is RUNNING"
    systemctl status eve-manager 2>&1 | head -5 | sed 's/^/  /'
else
    print_warning "eve-manager service is NOT running"
    print_warning "Recent logs:"
    journalctl -u eve-manager -n 10 --no-pager 2>/dev/null | sed 's/^/    /' || echo "    (no logs available)"
fi

# 11. Recommendations
print_header "Recommendations"
echo ""
echo "If pip is stuck:"
echo ""
echo "  1. Kill pip process:"
echo "     pkill -f 'pip install' || true"
echo ""
echo "  2. Try manual install:"
echo "     source $APP_DIR/venv/bin/activate"
echo "     pip install --default-timeout=120 --retries 10 requests"
echo ""
echo "  3. Use mirror:"
echo "     pip install -i https://mirrors.aliyun.com/pypi/simple/ \\"
echo "       --default-timeout=120 --retries 10 -r requirements.txt"
echo ""
echo "  4. Check available space:"
echo "     df -h $APP_DIR"
echo ""
echo "  5. View full pip output:"
echo "     pip install -r requirements.txt --verbose"
echo ""

print_success "Diagnostic complete!"
