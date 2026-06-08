# Eve - Xui Manager v1.9.2

## [1.9.2] - 2026-06-09

### 🐛 Bug Fixes

#### 🔧 3x-ui v3 Compatibility — Client Edit
- **Edit client روی سرورهای v3 حالا کار می‌کند**  
  قبلاً endpoint قدیمی `updateClient` در v3 حذف شده بود و 404 برمی‌گرداند.  
  حالا به‌طور خودکار از API جدید `POST /panel/api/clients/update/{email}` استفاده می‌شود.

#### 🔧 3x-ui v3 Compatibility — Bulk Operations
- **Bulk add/reduce volume و extend days روی v3 کار می‌کند**  
  توابع داخلی bulk (`_post_client_update`، `_reset_client_traffic_core`) v3 را detect کرده و از endpoint صحیح استفاده می‌کنند.

#### 🗑️ Delete Client — خطای بی‌معنی رفع شد
- **ارور واقعی نشان داده می‌شود**  
  قبلاً هر خطایی در حذف کلاینت به پیام ثابت `"Error deleting client"` تبدیل می‌شد.  
  حالا دلیل واقعی خطا (پیام پنل، HTTP status، network error) نمایش داده می‌شود.
- **Route اکنون همیشه JSON برمی‌گرداند**  
  در صورت exception ناخواسته، به‌جای صفحه HTML 500، پیام خطای JSON مناسب ارسال می‌شود.
- **v3 delete اکنون ownership را cleanup می‌کند**  
  پس از حذف موفق کلاینت در v3، رکوردهای ownership از DB پاک و cache invalidate می‌شود.

#### 🔄 Reset Traffic — Billable Volume اعمال نمی‌شد
- **حجم وارد شده در "Billable Volume" اکنون روی پنل اعمال می‌شود**  
  قبلاً `volume_gb` فقط برای محاسبه صورتحساب استفاده می‌شد و `totalGB` کلاینت تغییر نمی‌کرد — یوزر unlimited می‌ماند.  
  حالا پس از reset موفق، اگر volume مشخص شده باشد، `totalGB` کلاینت نیز آپدیت می‌شود.  
  - `volume_gb = 0` → فقط reset (unlimited می‌ماند)
  - `volume_gb = 50` → reset + حجم ۵۰ گیگ ست می‌شود

#### 💬 بهبود پیام‌های خطا در سراسر پنل
- **تمام عملیات‌های ناموفق اکنون جزئیات واقعی نشان می‌دهند:**
  - کدام endpoint شکست خورد
  - HTTP status code دقیق
  - پیام پنل (فیلد `msg` یا `message`)
- به‌جای `"Update failed"` یا `"Delete failed"` بی‌معنی

---

### 📋 تغییرات فنی

| فایل | تغییر |
|------|-------|
| `app.py` | v3 branch در `edit_client` route |
| `app.py` | v3 branch در `_post_client_update` (bulk) |
| `app.py` | v3 branch در `_reset_client_traffic_core` (bulk) |
| `app.py` | `_apply_volume_cap_after_reset` — تابع جدید برای ست کردن totalGB بعد از reset |
| `app.py` | `delete_client` route کاملاً در try/except |
| `app.py` | v3 delete path: ownership cleanup + log_transaction |
| `app.py` | خطاهای legacy endpoints با جزئیات HTTP status و پیام پنل |
| `templates/dashboard.html` | catch block در `submitDeleteClient` پیام واقعی نشان می‌دهد |
| `3XUI_V3_API.md` | **جدید** — مرجع کامل API های 3x-ui v3 |

---

### 🚀 نحوه آپدیت

```bash
git pull
pip install -r requirements.txt
python app.py
```

> ⚠️ این نسخه backward compatible است — هیچ migration دیتابیسی نیاز نیست.
