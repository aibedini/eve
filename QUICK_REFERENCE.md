# Quick Reference: Offline Installation

## 📋 For Iranian Servers (No Internet)

```bash
# MACHINE WITH INTERNET:
git clone https://github.com/yoyoraya/eve-xui-manager.git
cd eve-xui-manager
bash prepare-wheels.sh .
cd ..
zip -r eve-xui-manager.zip eve-xui-manager
# Transfer eve-xui-manager.zip to server via USB/SCP/SFTP

# ON SERVER (No Internet):
cd /root
bash setup.sh
# [1] Install
# [2] ZIP file
# (auto-detects wheels and installs offline)
```

## 🌐 For Servers with Limited Internet

```bash
# ON SERVER:
bash setup.sh
# Auto tries: Local wheels → PyPI → Aliyun mirror → Tsinghua mirror
# Tolerates slow connections (120s timeout, 10 retries)
```

## ⚠️ If Installation Fails

```bash
# Try Aliyun mirror
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  -r requirements.txt --default-timeout=120 --retries 10

# Or Tsinghua mirror  
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  -r requirements.txt --default-timeout=120 --retries 10
```

## 📦 What Changed in setup.sh

1. **Longer timeouts**: 120 seconds instead of 15
2. **More retries**: 10 attempts instead of default
3. **Offline support**: Detects wheels/ folder and uses `--no-index`
4. **Auto-fallback**: Mirror 1 → Mirror 2 → Original PyPI
5. **Better errors**: Clear messages when all methods fail

## 📄 New Files Added

- `prepare-wheels.sh` - Download all packages on internet machine
- `OFFLINE_INSTALL.md` - English guide
- `OFFLINE_INSTALL_FA.md` - Persian guide
- `QUICK_REFERENCE.md` - This file

## 🔧 How to Use New Features

### Without Internet (Recommended for Iran)
```bash
# Download wheels on another computer
bash prepare-wheels.sh /path/to/eve-xui-manager
# Creates: eve-xui-manager/wheels/

# Transfer & install on server
bash setup.sh  →  [1] Install  →  [2] ZIP file
```

### With Slow/Restricted Internet
```bash
# Just run - script auto-tries everything
bash setup.sh  →  [1] Install
```

## ✅ Features Added

| Feature | Before | After |
|---------|--------|-------|
| Timeout | 15s | 120s |
| Retries | default | 10x |
| Mirrors | none | Aliyun/Tsinghua |
| Offline | no | yes (wheels) |
| Error handling | fails | tries alternatives |

## 🚀 Performance Tips

1. **Best**: Download wheels on fast internet, use offline install
2. **Good**: Use server internet + mirror fallback
3. **OK**: Direct PyPI (may timeout in Iran)

## 📞 Troubleshooting

| Problem | Solution |
|---------|----------|
| Timeout | Increase timeout: `pip install --default-timeout=300 ...` |
| Network blocked | Use mirrors or offline wheels |
| One package fails | Try again with `--retries 10` |
| Service won't start | `journalctl -u eve-manager -f` |
