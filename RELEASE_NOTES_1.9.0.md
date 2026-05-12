# Eve - Xui Manager v1.9.0

## [1.9.0] - 2026-05-12

### ✨ New Features

#### Multi-Version Panel Compatibility
- **Flexible Login Protocol**: Added support for both JSON-body and form-encoded authentication methods
  - Automatically detects and adapts to different X-UI panel versions
  - 3x-ui v3.0.0+ (JSON login) and older panels (form-encoded) now both supported
  - Resolves login failures across diverse panel deployments

#### Self-Signed SSL Certificate Support
- **SSL Configuration Enhancements**:
  - Added validation for certificate and private key files
  - File existence and read-permission checks prevent configuration errors
  - New `ssl_status` field in API to indicate configuration state: `active`, `error`, or `not_configured`
  - Improved error messages guide users to fix permission issues

#### Nginx SSL Setup Improvements
- **Self-Signed Certificate Support in setup.sh**:
  - New `SELF_SIGNED_SSL_DIR` variable for self-signed certificate paths
  - Nginx configuration can now be provisioned for Let's Encrypt OR self-signed certificates
  - Better separation of certificate source logic

### 🔧 Technical Improvements

- **SSL/TLS**: Disabled urllib3 SSL warnings to reduce console clutter
- **Session Management**: Extended X-UI panel session timeout from 3s to 8s for more reliable authentication
- **Connection Handling**: Session object configured to skip SSL verification at session level, ensuring consistency across all requests including redirects
- **API Response Handling**: Improved error reporting with detailed messages for login failures
- **Code Quality**: Refined session caching logic and improved comments for maintainability

### 🐛 Bug Fixes

- Fixed SSL settings API to validate both certificate and key paths before saving
- Fixed issue where mismatched cert/key configurations could be partially saved
- Improved error clarity when SSL files are missing or unreadable

### 📝 Notes

- Changes maintain backward compatibility with existing configurations
- Panel connection logic is now more resilient to different deployment scenarios
- SSL configuration validation prevents common setup mistakes upfront
