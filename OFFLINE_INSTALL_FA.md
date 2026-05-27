# نصب آفلاین Eve X-UI Manager برای سرورهای ایران

## مشکل
هنگام نصب روی سرورهایی که دسترسی محدود یا بسته شده به PyPI دارند، pip تایم‌آوت می‌شود:
```
Connection to pypi.org timed out (connect timeout=15)
```

## ✅ حل شده

اسکریپت `setup.sh` اکنون:
- ✅ تایم‌آوت را **۱۲۰ ثانیه** می‌کند (بجای ۱۵)
- ✅ **۱۰ بار** تلاش می‌کند (بجای یکبار)
- ✅ **Mirror فال‌بک** (Aliyun → Tsinghua → PyPI)
- ✅ **نصب آفلاین** (اگر wheels موجود باشد)
- ✅ **تشخیص خودکار** مشکل

---

## راهحل‌ها

### گزینه 1: استفاده از Mirror PyPI (ساده‌ترین)

اگر سرور نوعی اینترنت دارد ولی PyPI رسمی بسته است:

```bash
# روی سرور (هنگام نصب)
bash setup.sh
# انتخاب: [1] Install
# پاسخ سوال Domain و دیگر گزینه‌ها
# اسکریپت خودکار Mirror را تلاش می‌کند
```

**Mirror های دسترسی‌پذیر:**
- Aliyun: `https://mirrors.aliyun.com/pypi/simple/`
- Tsinghua: `https://pypi.tuna.tsinghua.edu.cn/simple`

**اگر خودکار کار نکرد، دستی:**
```bash
source /opt/eve-xui-manager/venv/bin/activate
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  --default-timeout=120 --retries 10 -r requirements.txt
```

### گزینه 2: نصب آفلاین با Wheels (بهترین برای محیط‌های بسته)

#### مرحله 1: دانلود Wheels (بر روی ماشینی با اینترنت)

```bash
# 1. روی ماشین با اینترنت (لینوکس/Mac/WSL)
git clone https://github.com/yoyoraya/eve-xui-manager.git
cd eve-xui-manager

# 2. دانلود تمام packages
chmod +x prepare-wheels.sh
bash prepare-wheels.sh .

# یا دستی (اگر فایل نداشتید):
mkdir -p wheels
pip download -r requirements.txt -d wheels --default-timeout=120 --retries 10
pip wheel --wheel-dir wheels -r requirements.txt --no-build-isolation
```

نتیجه: پوشه `wheels/` با ۲۰+ فایل `.whl` ایجاد می‌شود

#### مرحله 2: ارسال به سرور

```bash
# 1. ایجاد ZIP شامل wheels
cd ..
zip -r eve-xui-manager.zip eve-xui-manager/

# 2. انتقال به سرور (از ترمینال محلی)
scp eve-xui-manager.zip root@SERVER_IP:/root/

# یا اگر SSH دسترسی نیست:
# از طریق SFTP/فایل منیجر ارسال کنید
```

**⚠️ مهم:** اطمینان حاصل کنید پوشه `wheels/` داخل ZIP است!

#### مرحله 3: نصب روی سرور

```bash
# بر روی سرور ایران
ssh root@SERVER_IP

# رفتن به دایرکتوری
cd /root
ls -lh eve-xui-manager.zip

# نصب
bash setup.sh

# انتخاب گزینه [1] Install
# سوالات:
#   Domain/IP: [IP سرور یا دومین]
#   Database: [1] PostgreSQL یا [2] SQLite
#   Install source: [2] ZIP file ← انتخاب این گزینه
#   ZIP location: [اتر یا مسیر] - اسکریپت خودکار پیدا می‌کند
```

✅ **نصب کامل بدون اینترنت!**

تمام dependencies از پوشه `wheels/` نصب می‌شوند.

### گزینه 3: نصب Hybrid (اینترنت + Wheels)

اگر سرور نوعی اینترنت دارد:

```bash
# ترکیب هر دو روش:
# - اسکریپت ابتدا wheels محلی را استفاده می‌کند
# - اگر کامل نبود، به صورت خودکار PyPI را تلاش می‌کند
# - اگر ناموفق بود، mirror چینی را تلاش می‌کند

# فقط اسکریپت را اجرا کنید:
bash setup.sh
```

---

## 🔧 اگر نصب گیر کرد

### بررسی وضعیت

```bash
# 1. دیاگنوز کامل
bash /opt/eve-xui-manager/diagnose.sh

# 2. بررسی لاگ‌های نصب
journalctl -u eve-manager -f -n 100

# 3. بررسی رفع مشکل بصری
systemctl status eve-manager
```

### اگر pip تایم‌آوت شد

```bash
# 1. بکشید pip را
pkill -f 'pip install' || true
ps aux | grep pip

# 2. دوباره سعی کنید با Mirror
source /opt/eve-xui-manager/venv/bin/activate
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  --default-timeout=120 --retries 10 -r /opt/eve-xui-manager/requirements.txt

# 3. یا اگر wheels دارید:
pip install --no-index --find-links=/opt/eve-xui-manager/wheels \
  -r /opt/eve-xui-manager/requirements.txt
```

### اگر Disk Space مشکل دار است

```bash
# بررسی فضای دیسک
df -h /opt/eve-xui-manager
du -sh /opt/eve-xui-manager/*

# پاک کردن غیرضروری
rm -rf /opt/eve-xui-manager/venv
pip cache purge
```

---

## 📋 راهنمای متقدم

### مشاهده لاگ‌های نصب

```bash
# خدمت را رصد کنید (زنده)
journalctl -u eve-manager -f

# یا بدون live update
journalctl -u eve-manager -n 50 --no-pager

# یا همه لاگ‌ها
journalctl -u eve-manager > /tmp/eve-logs.txt
cat /tmp/eve-logs.txt
```

### اگر نیاز به wheels جدید باشد

```bash
# روی سرور - اگر نیاز به wheels جدید:

# 1. دانلود مجدد (با اینترنت موقتی یا VPN)
cd /opt/eve-xui-manager
source venv/bin/activate
pip install --default-timeout=120 --retries 10 -r requirements.txt

# 2. یا استفاده از mirror
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  --default-timeout=120 --retries 10 -r requirements.txt
```

### بررسی dependencies

```bash
# بررسی نسخه‌های نصب شده
source /opt/eve-xui-manager/venv/bin/activate
pip list

# بررسی اگر چیزی از دست رفته
pip install --dry-run -r requirements.txt

# اگر یک package خاص مشکل دار است:
pip install --default-timeout=120 --retries 10 flask==3.1.2
```

### Uninstall و دوباره نصب

```bash
# اگر کاملاً خراب شد:
systemctl stop eve-manager

# حذف venv
rm -rf /opt/eve-xui-manager/venv

# دوباره نصب
bash /opt/eve-xui-manager/setup.sh
# انتخاب [1] Install
```

---

## نسخه‌های قدیمی setup.sh

اگر فایل `prepare-wheels.sh` ندارید، این دستور کافی است:

```bash
# بر روی ماشین با اینترنت
mkdir -p wheels
pip download -r requirements.txt -d wheels --default-timeout=120 --retries 10
pip wheel --wheel-dir wheels -r requirements.txt --no-build-isolation

# یا استفاده از requirements.txt مستقیم بدون فایل
pip download \
  flask==3.1.2 \
  flask-limiter==4.0.0 \
  flask-sqlalchemy==3.1.1 \
  gunicorn==23.0.0 \
  jdatetime==5.2.0 \
  pillow==12.0.0 \
  qrcode==8.2 \
  requests==2.32.5 \
  -d wheels --default-timeout=120 --retries 10
```

---

## سوالات متداول

**Q: چند مدت طول می‌کشد؟**
- Hybrid (mirrors): ۵-۱۰ دقیقه
- Offline: ۲-۳ دقیقه
- اگر gیر کرد: `Ctrl+C` و دوباره شروع کنید

**Q: حجم wheels چقدر است؟**
- حدود ۳۰-۵۰ MB
- ZIP فشرده: ۱۰-۲۰ MB

**Q: آیا باید تمام packages دانلود کنم؟**
- بله، برای پایایی بهتر است تمام dependencies دانلود شود
- میزانی که لیست `requirements.txt` مشخص می‌کند

**Q: اگر یک پکیج ناموفق باشد؟**
- اسکریپت خودکار mirror را تلاش می‌کند
- اگر باز هم ناموفق بود، نصب متوقف می‌شود و پیغام خطا نمایش می‌دهد
- دستی: `pip install package-name --default-timeout=120 --retries 10`

**Q: آیا می‌تونم نصب و پاک‌سازی را تکرار کنم؟**
- بله! اسکریپت `setup.sh` safe است
- database و .env محفوظ می‌شود
- تنها source code به روز می‌شود

**Q: خدمت شروع نمی‌شود**
- بررسی کنید: `journalctl -u eve-manager -f -n 100`
- یا: `bash /opt/eve-xui-manager/diagnose.sh`

---

## پشتیبانی

اگر مشکلی دارید:

```bash
# لاگ‌های جزئی
journalctl -u eve-manager -n 100

# بررسی وضعیت خدمت
systemctl status eve-manager

# راه‌اندازی دوباره
systemctl restart eve-manager

# دیاگنوز
bash /opt/eve-xui-manager/diagnose.sh
```

---

## مراجع

- [setup.sh](./setup.sh) - اسکریپت نصب اصلی (اصلاح شده)
- [prepare-wheels.sh](./prepare-wheels.sh) - اسکریپت دانلود wheels
- [diagnose.sh](./diagnose.sh) - اسکریپت تشخیصی (جدید)
- [requirements.txt](./requirements.txt) - لیست dependencies
- [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) - مرجع سریع


### گزینه 1: استفاده از Mirror PyPI (ساده‌ترین)

اگر سرور نوعی اینترنت دارد ولی PyPI رسمی بسته است، از mirror چینی استفاده کنید:

```bash
# روی سرور (هنگام نصب)
bash setup.sh
# انتخاب: [1] Install
# پاسخ سوال Domain و دیگر گزینه‌ها
# اسکریپت خودکار Aliyun/Tsinghua mirror را تلاش می‌کند
```

**mirror های دسترسی‌پذیر:**
- Aliyun: `https://mirrors.aliyun.com/pypi/simple/`
- Tsinghua: `https://pypi.tuna.tsinghua.edu.cn/simple`

### گزینه 2: نصب آفلاین با Wheels (بهترین برای محیط‌های بسته)

#### مرحله 1: دانلود Wheels (بر روی ماشینی با اینترنت)

```bash
# 1. روی ماشین با اینترنت (لینوکس/Mac/WSL)
git clone https://github.com/yoyoraya/eve-xui-manager.git
cd eve-xui-manager

# 2. دانلود تمام packages
chmod +x prepare-wheels.sh
bash prepare-wheels.sh

# یا اگر نسخه شما ندارد:
mkdir -p wheels
pip download -r requirements.txt -d wheels --default-timeout=120 --retries 10
pip wheel --wheel-dir wheels -r requirements.txt --no-build-isolation
```

نتیجه: پوشه `wheels/` با ۲۰+ فایل `.whl` ایجاد می‌شود

#### مرحله 2: ارسال به سرور

```bash
# 1. ایجاد ZIP شامل wheels
cd ..
zip -r eve-xui-manager.zip eve-xui-manager/

# 2. انتقال به سرور (از ترمینال محلی)
scp eve-xui-manager.zip root@SERVER_IP:/root/

# یا اگر SSH دسترسی نیست:
# از طریق SFTP/فایل منیجر ارسال کنید
```

#### مرحله 3: نصب روی سرور

```bash
# بر روی سرور ایران
ssh root@SERVER_IP

# رفتن به دایرکتوری
cd /root
ls -lh eve-xui-manager.zip

# نصب
bash setup.sh

# انتخاب گزینه [1] Install
# سوالات:
#   Domain/IP: [IP سرور]
#   Passwords: [کلمات عبور]
#   Install source: [2] ZIP file ← انتخاب این گزینه
#   Database: [1] PostgreSQL یا [2] SQLite
```

**نکات مهم:**
- اطمینان حاصل کنید پوشه `wheels/` داخل ZIP باشد
- اسکریپت خودکار ZIP را پیدا می‌کند (`/root/eve-xui-manager.zip`)
- تمام dependencies بدون اینترنت نصب می‌شود

### گزینه 3: نصب Hybrid (اینترنت + Wheels)

اگر سرور نوعی اینترنت دارد:

```bash
# ترکیب هر دو روش:
# - اسکریپت ابتدا wheels محلی را استفاده می‌کند
# - اگر کامل نبود، به صورت خودکار PyPI را تلاش می‌کند
# - اگر ناموفق بود، mirror چینی را تلاش می‌کند

# فقط اسکریپت را اجرا کنید:
bash setup.sh
```

---

## راهنمای متقدم

### مشاهده لاگ‌های نصب

```bash
# خدمت را رصد کنید
journalctl -u eve-manager -f

# یا فایل لاگ‌های pip
cat /opt/eve-xui-manager/venv/bin/pip
```

### اگر نصب ناقص ماند

```bash
# روی سرور - اگر نیاز به wheels جدید باشد:

# 1. دانلود مجدد (با اینترنت موقتی یا VPN)
cd /opt/eve-xui-manager
source venv/bin/activate
pip install --default-timeout=120 --retries 10 -r requirements.txt

# 2. یا استفاده از mirror
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
  --default-timeout=120 --retries 10 -r requirements.txt
```

### بررسی dependencies

```bash
# بررسی نسخه‌های نصب شده
source /opt/eve-xui-manager/venv/bin/activate
pip list

# بررسی اگر چیزی از دست رفته
pip install --dry-run -r requirements.txt
```

---

## فیلتر‌شدگی اینترنت؟

اگر هیچ اینترنتی ندارید:

1. **محلی**: روی یک ماشین با لینوکس/Mac دانلود کنید
2. **محدود**: از mirrors استفاده کنید (Aliyun/Tsinghua)
3. **آفلاین**: wheels را دانلود کرده و ارسال کنید

---

## نسخه‌های قدیمی setup.sh

اگر فایل `prepare-wheels.sh` ندارید، این دستور کافی است:

```bash
# بر روی ماشین با اینترنت
mkdir -p wheels
pip download -r requirements.txt -d wheels --default-timeout=120 --retries 10
pip wheel --wheel-dir wheels -r requirements.txt --no-build-isolation

# یا استفاده از requirements.txt مستقیم بدون فایل
pip download \
  flask==3.1.2 \
  flask-limiter==4.0.0 \
  flask-sqlalchemy==3.1.1 \
  gunicorn==23.0.0 \
  jdatetime==5.2.0 \
  pillow==12.0.0 \
  qrcode==8.2 \
  requests==2.32.5 \
  -d wheels --default-timeout=120 --retries 10
```

---

## سوالات متداول

**Q: چند مدت طول می‌کشد؟**
- Hybrid: ۵-۱۰ دقیقه (ممکن است بستگی به اینترنت داشته باشد)
- Offline: ۲-۳ دقیقه (بدون اینترنت)

**Q: حجم wheels چقدر است؟**
- حدود ۳۰-۵۰ MB
- ZIP فشرده: ۱۰-۲۰ MB

**Q: آیا باید تمام packages دانلود کنم؟**
- بله، برای پایایی بهتر است تمام dependencies دانلود شود
- میزانی که لیست `requirements.txt` مشخص می‌کند

**Q: اگر یک پکیج ناموفق باشد؟**
- اسکریپت خودکار mirror را تلاش می‌کند
- اگر باز هم ناموفق بود، نصب متوقف می‌شود و پیغام خطا نمایش می‌دهد

---

## پشتیبانی

اگر مشکلی دارید:

```bash
# لاگ‌های جزئی
journalctl -u eve-manager -n 100

# بررسی وضعیت خدمت
systemctl status eve-manager

# راه‌اندازی دوباره
systemctl restart eve-manager
```

## مراجع

- [setup.sh](./setup.sh) - اسکریپت نصب اصلی
- [prepare-wheels.sh](./prepare-wheels.sh) - اسکریپت دانلود wheels
- [requirements.txt](./requirements.txt) - لیست dependencies
