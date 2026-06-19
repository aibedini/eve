# Changelog - Eve

All notable changes to Eve - Xui Manager are documented in this file.

## [Unreleased]

### 🐛 Bug Fixes & Improvements
- **3x-ui v3.3.1 compatibility (CSRF)**: v3.3.1 added a CSRF middleware in front of `POST /login` (and every other cookie-session state-changing route), so EVE could no longer log in to upgraded panels — cookie-login servers returned `403`, which surfaced as `502 Bad Gateway` on the EVE subscription page when the client wasn't cached. EVE now fetches a token from `GET {basePath}/csrf-token` and pins it as the `X-CSRF-Token` header on the panel session before logging in, so login and all later `/panel/api/*` POSTs (add/update/delete client, reset traffic, backup, onlines) pass the guard. Verified live against a v3.3.1 panel (login `success:true` with the token, `403` without it). Fully backward compatible: older panels (≤3.3.0, v3, pre-v3) have no `/csrf-token` route and ignore the header, and API-token (Bearer) servers are unaffected (CSRF is bypassed for token auth).
- **HTTPS-only panel self-heal**: A server saved with an `http://` host pointing at an SSL-enabled panel (HSTS + Secure cookies) failed with a bare `ConnectionError` shown as "Error testing connection". Testing a server now auto-detects this and rewrites the host to `https://` when — and only when — https answers and http does not, so plaintext panels are left untouched.

## [1.4.2] - 2025-12-12

### 🐛 Bug Fixes & Improvements
- **Reseller Visibility**: Fixed issue where clients were hidden from resellers due to missing inbound IDs (implemented loose matching).
- **Traffic Formatting**: Improved traffic display to dynamically show KB/MB/GB/TB units.
- **UI Alignment**: Fixed action button alignment on desktop (right-aligned) and mobile (left-aligned).
- **Server List**: Fixed bug where server list in modals would be empty after status updates.
- **Search Autofill**: Implemented fix to prevent browser autofill on the search input.

## [1.4.1] - 2025-12-11

### ✨ Protocol Link Support
- Full support for direct client links for all 3x-ui protocols (vmess, vless, trojan, shadowsocks) with proper ws/grpc/tcp, TLS/Reality, and plugin parameters.
- Improved link generation logic for all supported protocols.

### 🐛 Bug Fixes & Improvements
- Webpath fixes for custom panel paths (login, API, panel URLs).
- Expiry display and UI tweaks.
- Version and tag update logic improvements.

## [1.3.0] - 2025-12-09

### ✨ New Features
- **FAQ Platform Support**: Added ability to categorize FAQs by platform (Android, iOS, Windows).
- **FAQ Editor**: Enhanced FAQ editor with RTL/LTR support and improved toolbar.
- **Subscription Page**: Added platform filtering for Apps and FAQs.

### 🎨 UI/UX Improvements
- **Upload UI**: Redesigned file upload inputs with a modern button-and-spinner style.
- **Dropdowns**: Standardized OS and Platform selection to use consistent dropdown components on the Subscription page.
- **Icons**: Added platform-specific icons to selection menus.

## [1.2.1] - 2025-12-06

### ✨ New Features
- **Settings Page**: Introduced a dedicated settings area for managing application configurations.
- **Notification Templates**: Added a system to create and manage dynamic text templates for client creation notifications.
- **Backup & Restore**: Implemented full database backup and restore functionality with download/delete options.

### 🎨 UI/UX Improvements
- **Card Design**: Updated template management to use a modern card-based layout.
- **Number Formatting**: Applied global thousands separators for better readability of prices and volumes.
- **Visual Polish**: Improved button styles, spacing, and hover effects in the Settings and Backup sections.

## [1.2.0] - 2025-12-06

### ✨ New Features
- **Version Checking**: Added automatic version checking against GitHub Releases.
- **New Client Modal**: Enhanced success modal with QR codes and copyable subscription details.
- **Transaction Logging**: Expanded transaction logging to include Admin actions when costs are involved.

### 🎨 UI/UX Improvements
- **Renew Modal**: Redesigned to match the Purchase modal layout for consistency.
- **Receipts UI**: Improved card selection with a grid layout and copy-to-clipboard functionality.
- **Typography**: Standardized Persian text using the "Vazirmatn" font.
- **Sidebar**: Added a "New Release" badge with visual indicators.

### 🐛 Bug Fixes
- Fixed `TemplateAssertionError` in `base.html`.
- Resolved issue where Admin transactions were not being logged in history.

## [1.0.0] - 2024-12-01

### 🎉 Initial Release

This is the first stable release of Eve - Xui Manager with comprehensive features for managing multiple X-UI VPN panels.

### ✨ Features

#### Security
- Rate limiting: 5 login attempts per minute to prevent brute-force attacks
- Secure cookies with HTTPONLY and SAMESITE flags
- PBKDF2 password hashing with salt
- Failed login attempt logging with IP addresses
- Environment-based configuration for sensitive credentials
- Session timeout after 7 days
- Superadmin role for admin management

#### Dashboard
- Multi-server support (unlimited X-UI panels)
- Auto-detection of panel types (Sanaei 3X-UI vs Alireza X-UI)
- Real-time statistics: servers, inbounds, clients, traffic
- Responsive sidebar navigation
- Mobile-friendly hamburger menu
- Configurable auto-refresh intervals
- Manual refresh button

#### Client Management
- Enable/disable clients
- Reset client traffic
- Renew clients with configurable days and volume
- "Start after first use" option for subscriptions
- 3-Type QR Codes per client:
  - Subscription QR Code (Copy Sub)
  - Subscription JSON QR Code (Copy JSON)
  - Direct Connection Link QR Code (Copy Direct)

#### Server Configuration
- Add/edit/delete X-UI servers
- Customizable subscription paths per server
- Customizable JSON paths per server
- Custom subscription ports (with fallback to panel port)
- Connection testing

#### Admin Management
- Create/edit/disable admin accounts
- Superadmin can manage other admins
- Last login timestamp tracking
- Enable/disable accounts without deletion

#### UI/UX
- Professional dark theme
- Responsive grid layouts
- Color-coded expiry badges (green/yellow/red)
- Jalali calendar dates
- Traffic display (upload ↑ / download ↓)
- Volume information (used / total)
- Optimized for mobile devices
- Touch-friendly buttons and spacing

### 🔧 Technical

#### Backend
- Python 3.11 with Flask framework
- PostgreSQL database with connection pooling
- Flask-Limiter for rate limiting
- Werkzeug for security features
- QR code generation with python-qrcode
- Jdatetime for Jalali calendar support

#### Frontend
- HTML5 with semantic markup
- CSS3 with CSS variables for theming
- Vanilla JavaScript (no framework dependencies)
- Responsive grid and flexbox layouts
- SVG icons for cross-browser compatibility

#### API
- RESTful JSON API
- Secure session-based authentication
- Login rate limiting (5/min)
- Global rate limits (200/day, 50/hour)

### 📋 Database
- Admins table with superadmin role support
- Servers table with full X-UI panel configuration
- PostgreSQL with secure connection pooling
- Pre-ping health checks for database connections

### 🚀 Deployment Ready
- Environment variable configuration
- PBKDF2 password hashing
- Secure cookie settings
- Failed attempt logging
- Session management with secure flags

### 📱 Responsive Design
- Desktop: 3-column QR code grid
- Tablet (1024px): 2-column grid
- Mobile (768px): 1-column grid with icon-only buttons
- Mobile header with auto-height flex wrapping
- Touch-optimized interface

### 🔒 Security Features Implemented
1. Rate limiting (5 attempts/minute)
2. Secure cookies (HTTPONLY, SAMESITE=Lax)
3. Password hashing (PBKDF2)
4. Failed login logging
5. Environment-based configuration
6. Session timeout (7 days)
7. Admin role-based access
8. Database connection pooling with health checks

### ✅ Quality Assurance
- Tested with Sanaei 3X-UI panels
- Tested with Alireza X-UI panels
- Mobile responsive testing
- Security hardening completed
- Performance optimized with connection pooling

### 📚 Documentation
- Comprehensive README.md
- Technical documentation in replit.md
- API endpoint documentation
- Configuration guide
- Security best practices

### 🐛 Known Limitations
- None at release

### 🙏 Special Thanks

This project was built with careful attention to:
- Enterprise security practices
- User experience across all device sizes
- Performance and reliability
- Clean, maintainable code

---

## Release Schedule

- **1.0.0** - December 1, 2024 (Current)

For feature requests and bug reports, please visit the GitHub issues page.
