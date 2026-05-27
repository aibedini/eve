# 🆘 اگر Setup گیر کرد - راه حل فوری

اگر اسکریپت نصب در **Step 7: Python virtual environment** متوقف شد و دیگر پیش نمی‌رود:

## 🚨 فوری (۵ دقیقه)

```bash
# از ترمینال جدید بر روی سرور:
sudo bash /opt/eve-xui-manager/quick-fix.sh
```

این اسکریپت:
- ✅ pip processes گیر کرده را کشتار می‌کند
- ✅ دوباره pip را upgrade می‌کند
- ✅ requirements.txt را تمام نصب می‌کند
- ✅ مرور (Mirror) خودکار را تلاش می‌کند
- ✅ خدمت را restart می‌کند

---

## 🔍 اگر باز هم کار نکرد

### Step 1: بررسی وضعیت
```bash
bash /opt/eve-xui-manager/diagnose.sh
```

### Step 2: بررسی لاگ‌ها
```bash
journalctl -u eve-manager -f -n 100
```

### Step 3: دستی نصب
```bash
source /opt/eve-xui-manager/venv/bin/activate
pip install --default-timeout=120 --retries 10 -r /opt/eve-xui-manager/requirements.txt
```

---

## 💡 نکات

- اسکریپت `quick-fix.sh` **نمی‌خورد** و **نمی‌پاکد** چیزی
- فقط requirements.txt را از نو نصب می‌کند
- اگر wheels/ دارید، آن را استفاده می‌کند
- اگر wheels نبود، Mirror‌ها را تلاش می‌کند

---

## مستندات

- [INSTALLATION.md](./INSTALLATION.md) - راهنمای کامل
- [OFFLINE_INSTALL_FA.md](./OFFLINE_INSTALL_FA.md) - راهنمای فارسی
- [OFFLINE_INSTALL.md](./OFFLINE_INSTALL.md) - English guide
- [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) - مرجع سریع
