# Eve X-UI Manager - Offline Installation Guide

> برای کاربران ایران و سرورهای بدون/محدود اینترنت

## 🚀 Quick Start (سریع‌ترین راه)

```bash
# Step 1: Download wheels on a computer with internet
bash prepare-wheels.sh /path/to/eve-xui-manager
zip -r eve-xui-manager.zip eve-xui-manager
scp eve-xui-manager.zip root@SERVER:/root/

# Step 2: On server (no internet needed)
bash setup.sh
# [1] Install
# [2] ZIP file
```

✅ **Done in 3-5 minutes!**

---

## 📋 Available Scripts

### Installation & Setup
| Script | Purpose | When to Use |
|--------|---------|------------|
| `setup.sh` | Main installer (UPDATED) | Fresh install or update |
| `quick-fix.sh` | Fix stuck installations | If setup.sh hangs at Step 7 |
| `prepare-wheels.sh` | Download all packages | Before moving project to offline server |

### Troubleshooting
| Script | Purpose | When to Use |
|--------|---------|------------|
| `diagnose.sh` | Check installation status | After installation or if service won't start |

---

## 🔧 What's Fixed in This Version

| Issue | Before | After |
|-------|--------|-------|
| **Timeout** | 15 seconds (too short) | 120 seconds (Iran-friendly) |
| **Retries** | No retry logic | 10 automatic retries |
| **Stuck pip** | Gets stuck with no feedback | Shows progress, detects hanging |
| **Mirror support** | None | Aliyun + Tsinghua automatic fallback |
| **Offline mode** | Not supported | Full offline with wheels/ folder |
| **Error handling** | Fails silently | Clear error messages with next steps |

---

## 🛠️ Usage Scenarios

### Scenario 1: Server with NO Internet (Completely Offline)

**Step 1: On a computer WITH internet**
```bash
git clone https://github.com/yoyoraya/eve-xui-manager.git
cd eve-xui-manager
bash prepare-wheels.sh .
# Creates: wheels/ folder with all packages
```

**Step 2: Transfer to offline server**
```bash
cd ..
zip -r eve-xui-manager.zip eve-xui-manager/
# Copy eve-xui-manager.zip to USB/external drive
# Transfer to offline server via USB or external drive
```

**Step 3: On offline server (NO internet)**
```bash
cd /root
bash setup.sh
# [1] Install
# [2] ZIP file (auto-detects wheels/)
```

✅ All packages installed from wheels/ folder
⏱️ Time: ~3-5 minutes

---

### Scenario 2: Server with SLOW Internet (Iran, etc.)

**Direct installation with automatic mirror fallback**
```bash
bash setup.sh
# [1] Install
# [1] GitHub (or [2] ZIP if you have it)
```

**Script automatically tries:**
1. PyPI with 120s timeout + 10 retries
2. Aliyun mirror if PyPI fails
3. Tsinghua mirror if Aliyun fails
4. Helpful error message if all fail

✅ Tolerates slow/restricted networks
⏱️ Time: 10-20 minutes (depending on internet)

---

### Scenario 3: Installation Got Stuck

If `setup.sh` hangs at **Step 7: Python virtual environment**:

```bash
# In another terminal:
sudo bash quick-fix.sh
```

This script:
- Kills stuck pip processes
- Retries installation with proper error handling
- Shows verbose output so you can see progress
- Tries all mirrors automatically

✅ Usually fixes in 2-3 minutes

---

## 🚨 Troubleshooting

### "Connection to pypi.org timed out"

**If it keeps failing:**
```bash
# Option A: Use mirror directly
source /opt/eve-xui-manager/venv/bin/activate
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  --default-timeout=120 --retries 10 -r requirements.txt

# Option B: Use offline wheels (if available)
pip install --no-index --find-links=/opt/eve-xui-manager/wheels \
  -r /opt/eve-xui-manager/requirements.txt
```

### Service won't start

```bash
# Check what's wrong
bash /opt/eve-xui-manager/diagnose.sh

# View logs
journalctl -u eve-manager -f -n 50

# Restart service
systemctl restart eve-manager
```

### "Missing package X"

```bash
# Install individual package
source /opt/eve-xui-manager/venv/bin/activate
pip install --default-timeout=120 --retries 10 package-name==version
```

---

## 📊 Performance Tips

| Method | Speed | Requires Internet | Notes |
|--------|-------|-------------------|-------|
| **Offline wheels** | ⚡ 3-5 min | NO | Best for Iran |
| **Hybrid (mirrors)** | ⚡⚡ 10-20 min | YES (slow) | Auto-fallback |
| **Direct PyPI** | ⚡⚡⚡ 5-15 min | YES (fast) | Works in some regions |

---

## 🔍 What's in the Package

```
eve-xui-manager/
├── setup.sh              # Main installer (UPDATED ✓)
├── quick-fix.sh          # Quick fix script (NEW ✓)
├── prepare-wheels.sh     # Wheel downloader (NEW ✓)
├── diagnose.sh           # Diagnostic tool (NEW ✓)
├── OFFLINE_INSTALL.md    # English guide (NEW ✓)
├── OFFLINE_INSTALL_FA.md # Persian guide (NEW ✓)
├── QUICK_REFERENCE.md    # Quick reference (NEW ✓)
├── wheels/               # Python packages (optional)
│   ├── flask-3.1.2-py3-none-any.whl
│   ├── gunicorn-23.0.0-py3-none-any.whl
│   └── ... (20+ more files)
├── app.py                # Main app
├── requirements.txt      # Dependencies
└── ... (other files)
```

---

## 🎯 FAQ

**Q: Can I use the same wheels on different servers?**
A: Yes! Pure Python packages work everywhere. Binary packages (psycopg2, Pillow) need matching Python version & OS.

**Q: How big is the ZIP?**
A: ~50-100 MB uncompressed, ~20-30 MB compressed with wheels.

**Q: Can I update later without wheels?**
A: Yes! Use `setup.sh` option [2] Update with mirrors.

**Q: Is offline installation secure?**
A: Yes! Wheels are just Python packages, no different from pip install.

**Q: Do I need to regenerate wheels every time?**
A: No! Use same wheels for multiple installations (if Python version matches).

---

## 📞 Support

If something goes wrong:

```bash
# 1. Run diagnostics
bash /opt/eve-xui-manager/diagnose.sh

# 2. Check logs
journalctl -u eve-manager -f

# 3. Try quick fix
sudo bash /opt/eve-xui-manager/quick-fix.sh

# 4. Save logs for support
journalctl -u eve-manager > /tmp/eve-logs.txt
```

Then share the output with support.

---

## 🔄 Changelog

### Version 1.0 (Current - May 27, 2026)

**New Files:**
- ✅ `quick-fix.sh` - Emergency fix script
- ✅ `prepare-wheels.sh` - Wheel downloader
- ✅ `diagnose.sh` - Diagnostic tool
- ✅ `OFFLINE_INSTALL.md` - Full guide
- ✅ `OFFLINE_INSTALL_FA.md` - Persian guide
- ✅ `QUICK_REFERENCE.md` - Quick ref

**Updated Files:**
- ✅ `setup.sh` - Complete pip installation overhaul
  - Timeout: 15s → 120s
  - Retries: none → 10x
  - Offline support: NO → YES
  - Mirror fallback: NO → YES (Aliyun + Tsinghua)

---

## 🌐 Mirror Availability

| Mirror | URL | Region | Status |
|--------|-----|--------|--------|
| Aliyun | `https://mirrors.aliyun.com/pypi/simple/` | China | ✅ Fast |
| Tsinghua | `https://pypi.tuna.tsinghua.edu.cn/simple` | China | ✅ Fast |
| Official PyPI | `https://pypi.org/simple/` | Global | ⚠️ May be slow |

All are automatically tried by the installer.

---

## 📝 Notes

- All scripts are POSIX-compliant (work on any Linux/Unix)
- Tested on Ubuntu 20.04, 22.04, and Debian
- Requires: bash, curl, git, python3, pip, unzip
- No changes needed to app code (100% backward compatible)

---

## 🎓 For Developers

### Adding New Dependencies

```bash
# 1. Add to requirements.txt
echo "new-package==1.0.0" >> requirements.txt

# 2. Update wheels
bash prepare-wheels.sh .

# 3. Commit and push
git add requirements.txt wheels/
git commit -m "Add new-package"
git push
```

### Building for offline deployment

```bash
# Include wheels in package
git add wheels/
git commit -m "Include offline wheels"

# Create release ZIP
zip -r eve-xui-manager-offline-1.0.zip eve-xui-manager/

# Distribute the ZIP to target servers
```

---

## 📄 License

Same as Eve X-UI Manager project.

---

**Last Updated:** May 27, 2026  
**For Issues:** Check `diagnose.sh` output or see OFFLINE_INSTALL_FA.md
