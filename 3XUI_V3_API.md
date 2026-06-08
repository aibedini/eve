# 3x-ui v3 API Reference

**Base:** `/panel/api/*`  
**Auth:** `Authorization: Bearer <token>` یا session cookie (از `/login`)  
**Response shape:** `{"success": bool, "msg": "...", "obj": ...}`

---

## Authentication

| Method | Path | Description |
|--------|------|-------------|
| POST | `/login` | Login با username+password → session cookie |
| POST | `/logout` | پاک کردن session cookie |
| GET | `/csrf-token` | دریافت CSRF token (Bearer callers نیازی ندارن) |
| POST | `/getTwoFactorEnable` | آیا 2FA فعاله؟ |

---

## Inbounds `/panel/api/inbounds`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/list` | لیست کامل inbound‌ها + clientStats |
| GET | `/list/slim` | لیست slim (بدون uuid/password — برای صفحه لیست) |
| GET | `/options` | پیکر سبک برای dropdown: id, remark, protocol, port |
| GET | `/get/{id}` | یک inbound کامل |
| POST | `/add` | ساخت inbound جدید |
| POST | `/del/{id}` | حذف inbound |
| POST | `/bulkDel` | حذف چند inbound |
| POST | `/update/{id}` | جایگزینی کامل inbound |
| POST | `/setEnable/{id}` | فقط toggle enable (سریع‌تر از update) |
| POST | `/{id}/resetTraffic` | صفر کردن traffic inbound |
| POST | `/{id}/delAllClients` | حذف همه client‌های یک inbound |
| POST | `/resetAllTraffics` | صفر کردن traffic همه inbound‌ها |
| POST | `/import` | import inbound از JSON blob (form field: `data`) |
| GET | `/{id}/fallbacks` | لیست fallback rules |
| POST | `/{id}/fallbacks` | جایگزینی fallback list → Xray restart |

---

## Clients `/panel/api/clients`  ← **اصلی در v3**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/list` | لیست همه client‌ها + inbound IDs + traffic |
| GET | `/list/paged` | pagination + filter + sort (slim, بدون uuid/pass) |
| GET | `/get/{email}` | یک client کامل + inbound IDs |
| POST | `/add` | ساخت client جدید و attach به inbound‌ها — body: `{client, inboundIds}` |
| POST | `/update/{email}` | آپدیت client (کامل replace) — به همه inbound‌های attached اعمال میشه |
| POST | `/del/{email}` | حذف client از همه inbound‌ها (`?keepTraffic=1` برای نگه داشتن آمار) |
| POST | `/{email}/attach` | attach client به inbound‌های بیشتر |
| POST | `/{email}/detach` | detach client از inbound‌ها بدون حذف |
| POST | `/resetAllTraffics` | صفر کردن traffic همه client‌ها |
| POST | `/delDepleted` | حذف client‌های منقضی/تمام‌شده |
| POST | `/bulkAdjust` | تغییر expiry/quota چند client — `{addDays, addBytes}` |
| POST | `/bulkDel` | حذف چند client (`keepTraffic=true`) |
| POST | `/bulkCreate` | ساخت چند client — body: `[{client, inboundIds}, ...]` |
| POST | `/bulkAttach` | attach چند client به چند inbound |
| POST | `/bulkDetach` | detach چند client از چند inbound |
| POST | `/bulkResetTraffic` | صفر کردن traffic چند client |
| POST | `/resetTraffic/{email}` | صفر کردن traffic یک client |
| POST | `/updateTraffic/{email}` | تنظیم دستی up/down |
| POST | `/ips/{email}` | لیست IP‌های متصل شده |
| POST | `/clearIps/{email}` | پاک کردن لیست IP |
| POST | `/onlines` | email client‌های آنلاین (deduped) |
| POST | `/onlinesByNode` | آنلاین‌ها گروه‌بندی شده بر اساس node |
| POST | `/activeInbounds` | inbound tag‌هایی که ترافیک داشتن، per node |
| POST | `/lastOnline` | map: `email → last-seen timestamp` |
| GET | `/traffic/{email}` | traffic counter یک client |
| GET | `/subLinks/{subId}` | لیست URL‌های پروتکل برای یک subId (JSON) |
| GET | `/links/{email}` | همه URL‌های یک client (vmess/vless/trojan/ss/hysteria) |

### Groups

| Method | Path | Description |
|--------|------|-------------|
| GET | `/groups` | لیست همه گروه‌ها + تعداد عضو |
| GET | `/groups/{name}/emails` | فقط email‌های یک گروه |
| POST | `/groups/create` | ساخت گروه خالی |
| POST | `/groups/rename` | تغییر نام گروه (همه client‌ها هم آپدیت میشن) |
| POST | `/groups/delete` | حذف گروه (client‌ها حذف نمیشن) |
| POST | `/groups/bulkAdd` | افزودن چند client به گروه |
| POST | `/groups/bulkRemove` | حذف label گروه از چند client |

---

## Server `/panel/api/server`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/status` | CPU/RAM/disk/network/Xray state (cache 2s) |
| GET | `/cpuHistory/{bucket}` | تاریخچه CPU |
| GET | `/history/{metric}/{bucket}` | time-series هر metric — `{t, v}[]` |
| GET | `/xrayMetricsState` | وضعیت metrics block در config |
| GET | `/xrayMetricsHistory/{metric}/{bucket}` | time-series Xray metrics |
| GET | `/xrayObservatory` | آخرین snapshot از observatory |
| GET | `/xrayObservatoryHistory/{tag}/{bucket}` | time-series observatory |
| GET | `/getXrayVersion` | لیست Xray نسخه‌های قابل نصب |
| GET | `/getPanelUpdateInfo` | آیا نسخه جدیدتر panel هست؟ |
| GET | `/getConfigJson` | Xray config فعلی (JSON) |
| GET | `/getDb` | دانلود SQLite DB (backup) |
| GET | `/getMigration` | دانلود migration file |
| GET | `/getNewUUID` | UUID v4 جدید |
| GET | `/getWebCertFiles` | مسیر cert/key فعلی panel |
| GET | `/getNewX25519Cert` | keypair جدید برای Reality |
| GET | `/getNewmldsa65` | ML-DSA-65 keypair (post-quantum) |
| GET | `/getNewmlkem768` | ML-KEM-768 keypair |
| GET | `/getNewVlessEnc` | VLESS encryption auth options |
| POST | `/stopXrayService` | توقف Xray |
| POST | `/restartXrayService` | restart Xray |
| POST | `/installXray/{version}` | نصب نسخه Xray (`latest` هم قبوله) |
| POST | `/updatePanel` | self-update panel |
| POST | `/updateGeofile` | refresh GeoIP/GeoSite |
| POST | `/updateGeofile/{fileName}` | refresh یک فایل geo |
| POST | `/logs/{count}` | آخرین N خط log panel |
| POST | `/xraylogs/{count}` | آخرین N خط log Xray |
| POST | `/importDB` | restore DB از SQLite file (multipart `db`) — panel restart |
| POST | `/getNewEchCert` | ECH keypair برای یک SNI |

---

## Nodes `/panel/api/nodes`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/list` | لیست node‌ها + health |
| GET | `/get/{id}` | یک node |
| GET | `/webCert/{id}` | cert/key مسیر روی node |
| POST | `/add` | اضافه کردن node — `{url, apiToken, remark}` |
| POST | `/update/{id}` | آپدیت node |
| POST | `/del/{id}` | حذف node |
| POST | `/setEnable/{id}` | pause/resume sync |
| POST | `/test` | test یک node بدون save |
| POST | `/certFingerprint` | SHA-256 cert یک node |
| POST | `/probe/{id}` | probe و آپدیت health |
| POST | `/updatePanel` | self-update روی node‌ها |
| GET | `/history/{id}/{metric}/{bucket}` | time-series metric یک node |

---

## Settings `/panel/setting`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/all` | همه تنظیمات panel |
| POST | `/defaultSettings` | تنظیمات پیش‌فرض |
| POST | `/update` | ذخیره همه تنظیمات |
| POST | `/updateUser` | تغییر username/password admin |
| POST | `/restartPanel` | restart کل process panel |
| GET | `/getDefaultJsonConfig` | default Xray config template |

### API Tokens

| Method | Path | Description |
|--------|------|-------------|
| GET | `/apiTokens` | لیست token‌ها (value هرگز برنمی‌گردد) |
| POST | `/apiTokens/create` | ساخت token — plaintext فقط یک بار نشون داده میشه |
| POST | `/apiTokens/delete/{id}` | حذف token |
| POST | `/apiTokens/setEnabled/{id}` | enable/disable token |

---

## Xray Config `/panel/xray`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/` | config template + inbound tags + outbound test URL |
| GET | `/getDefaultJsonConfig` | default Xray config |
| GET | `/getOutboundsTraffic` | traffic هر outbound |
| GET | `/getXrayResult` | آخرین stdout/stderr Xray |
| POST | `/update` | ذخیره Xray config template |
| POST | `/warp/{action}` | مدیریت Cloudflare Warp |
| POST | `/nord/{action}` | مدیریت NordVPN |
| POST | `/resetOutboundsTraffic` | reset traffic یک outbound (by tag) |
| POST | `/testOutbound` | test یک outbound config |

---

## Custom Geo `/panel/api/custom-geo`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/list` | لیست geo source‌ها |
| GET | `/aliases` | alias‌های قابل استفاده در routing |
| POST | `/add` | اضافه کردن geo source |
| POST | `/update/{id}` | آپدیت geo source |
| POST | `/delete/{id}` | حذف |
| POST | `/download/{id}` | re-download on demand |
| POST | `/update-all` | re-download همه |

---

## Backup

| Method | Path | Description |
|--------|------|-------------|
| POST | `/panel/api/backuptotgbot` | ارسال DB backup به Telegram |

---

## Subscription Server (پورت جداگانه، default: 10882)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/{subPath}{subid}` | base64 subscription links |
| GET | `/{jsonPath}{subid}` | JSON subscription |
| GET | `/{clashPath}{subid}` | Clash/Mihomo YAML |

---

## WebSocket

| Method | Path | Description |
|--------|------|-------------|
| GET | `/ws` | real-time updates — فقط با session cookie (Bearer پشتیبانی نمیشه) |

---

## نکات مهم v3

- **Client اول‌درجه:** در v3 client‌ها مستقل از inbound مدیریت میشن — endpoint‌های قدیمی مثل `updateClient`, `delClient` روی `/inbounds` حذف شدن و 404 برمیگردونن
- **Update client:** `POST /panel/api/clients/update/{email}` — body کامل client (replace، نه patch)
- **Add client:** `POST /panel/api/clients/add` — body: `{client: {...}, inboundIds: [1,2,3]}`
- **Attach/Detach:** برای تغییر inbound assignment بدون حذف client
- **Bearer token:** از Settings → Security → API Token — هرگز expire نمیشه
