# نصب آفلاین Eve X-UI Manager

این راهنما برای سرورهایی است که به GitHub، PyPI یا مخازن Ubuntu دسترسی پایدار ندارند.

## اینترنت محدود

اگر سرور به اینترنت دسترسی دارد ولی PyPI کند یا فیلتر است، نصب‌کننده را عادی اجرا کنید:

```bash
bash setup.sh
```

نصب‌کننده ابتدا فایل‌های محلی و سپس mirrorهای پشتیبان را امتحان می‌کند.

## نصب کاملاً آفلاین (پیشنهادی)

bundle را روی یک سیستم Linux/WSL دارای اینترنت بسازید. برای جلوگیری از ناسازگاری بسته‌های Ubuntu، بهتر است ابتدا مشخصات سرور مقصد را جمع‌آوری کنید.

### ۱. گرفتن مشخصات سرور مقصد

روی سرور آفلاین:

```bash
bash collect-offline-profile.sh
```

فایل ساخته‌شده‌ی `eve-offline-profile.txt` را به سیستم آنلاین منتقل کنید.

### ۲. ساخت bundle روی سیستم آنلاین

```bash
git clone https://github.com/aibedini/eve.git
cd eve-xui-manager
chmod +x prepare-offline-bundle.sh
bash prepare-offline-bundle.sh --profile /path/to/eve-offline-profile.txt .
```

خروجی اصلی:

```text
dist/eve-xui-manager-offline.tar.gz
```

این bundle شامل سورس برنامه، بسته‌های Ubuntu، Python 3.11 قابل‌حمل و wheelهای لازم است. معماری پشتیبانی‌شده در حال حاضر `amd64/x86_64` است.

### ۳. انتقال و نصب روی سرور آفلاین

```bash
scp dist/eve-xui-manager-offline.tar.gz root@SERVER_IP:/root/
ssh root@SERVER_IP
tar -xzf /root/eve-xui-manager-offline.tar.gz -C /root
cd /root/eve-xui-manager
sudo bash setup.sh
```

در منو گزینه‌ی زیر را انتخاب کنید:

```text
[o] Setup Full Offline
```

در این حالت نصب‌کننده فقط از فایل‌های داخل bundle استفاده می‌کند.

## ساخت بدون profile

اگر مشخصات سرور مقصد در دسترس نیست:

```bash
bash prepare-offline-bundle.sh .
```

ساخت دقیق با profile مطمئن‌تر است، به‌خصوص وقتی نسخه‌ی Ubuntu سیستم سازنده و سرور مقصد متفاوت باشد.

## عیب‌یابی

```bash
# وضعیت سرویس
systemctl status eve-manager

# لاگ زنده
journalctl -u eve-manager -f -n 100

# بررسی خودکار
bash /opt/eve-xui-manager/diagnose.sh
```

اگر نصب‌کننده از نبودن بسته یا ناسازگاری Ubuntu خبر داد، bundle را با profile همان سرور دوباره بسازید.

## منابع

- [راهنمای کامل انگلیسی](./OFFLINE_INSTALL.md)
- [راهنمای Docker و bundleهای Docker](./DOCKER.md)
- [نصب‌کننده](./setup.sh)
- [سازنده‌ی bundle](./prepare-offline-bundle.sh)
