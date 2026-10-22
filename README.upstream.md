# SecuShare

![SecuShare logo](frontend/public/logo.svg)

A secure, end-to-end encrypted file sharing web application. Files are encrypted client-side before upload, and decryption keys are embedded in share URLs (after `#` fragment) so the server never sees them.

## Table of Contents

- [Features](#features)
- [Technology Stack](#technology-stack)
- [Architecture Overview](#architecture-overview)
- [Development Setup](#development-setup)
- [Docker Compose (Dev & Production)](#docker-compose-dev--production)
- [Initial Setup Wizard](#initial-setup-wizard)
- [Admin Dashboard](#admin-dashboard)
- [Production Deployment](#production-deployment)
- [Configuration Reference](#configuration-reference)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)
- [Backup and Recovery](#backup-and-recovery)
- [Security Considerations](#security-considerations)
- [Security Policy](SECURITY.md)
- [Credits](#credits)
- [License](#license)

---

## Features

- **End-to-end encryption**: AES-256-GCM encryption performed entirely in the browser using the Web Crypto API
- **Zero-knowledge architecture**: The server never has access to encryption keys (keys are transmitted via URL fragment, which is never sent to the server)
- **Guest accounts**: Configurable storage for anonymous users with configurable session duration
- **Authenticated accounts**: Configurable storage quota with additional features
- **Admin dashboard**: Browser-based setup wizard and admin panel for managing users, settings, and system maintenance
- **Runtime configuration**: File size limits, storage quotas, email domain restrictions, and guest session duration are configurable at runtime via the admin dashboard
- **Password protection**: Optional password protection for shares with PBKDF2 key derivation
- **Link expiration**: Set expiration times for share links (1 hour, 24 hours, 7 days, 30 days, or never)
- **Download limits**: Limit the number of downloads per share
- **Integrity verification**: SHA-256 checksums verify file integrity after download

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| Frontend | React 19 + TypeScript + Vite + TailwindCSS |
| Backend | Go 1.21+ + Fiber framework |
| Database | SQLite 3 |
| Encryption | Web Crypto API (AES-256-GCM) |
| Authentication | OPAQUE PAKE + JWT-backed sessions (`auth_token` httpOnly cookie; Bearer supported) |
| Password Handling | OPAQUE (account auth), bcrypt (share passwords), PBKDF2 (client-side key wrapping) |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                  Browser                                      │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐          │
│  │   File Input    │───►│  AES-256-GCM    │───►│  Upload to      │          │
│  │                 │    │  Encryption     │    │  Server         │          │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘          │
│                                │                        │                    │
│                                ▼                        │                    │
│                    ┌─────────────────┐                 │                    │
│                    │  Encryption Key │                 │                    │
│                    │  (never leaves  │                 │                    │
│                    │   browser)      │                 │                    │
│                    └─────────────────┘                 │                    │
└────────────────────────────────────────────────────────┼────────────────────┘
                                                         │
                                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Server (Go + Fiber)                              │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐          │
│  │   HTTP API      │───►│  File Service   │───►│  Disk Storage   │          │
│  │   (Fiber)       │    │                 │    │  (encrypted)    │          │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘          │
│         │                       │                                            │
│         ▼                       ▼                                            │
│  ┌─────────────────┐    ┌─────────────────┐                                 │
│  │  Auth Service   │    │   SQLite DB     │                                 │
│  │  (JWT)          │    │   (metadata)    │                                 │
│  └─────────────────┘    └─────────────────┘                                 │
└─────────────────────────────────────────────────────────────────────────────┘

Share Link Format:
https://example.com/s/{shareId}#{encryptionKeyBase64}
                      │              │
                      │              └── URL fragment (NEVER sent to server)
                      └── Share ID (stored in database)
```

### Data Flow

1. **Upload**: File → Browser encrypts → Server stores encrypted blob + metadata (NOT the key)
2. **Share**: User creates share → Server stores share record → Browser generates link with key in fragment
3. **Download**: Recipient opens link → Browser extracts key from fragment → Downloads encrypted file → Decrypts locally

---

## Development Setup

### Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Go | 1.21+ | Required for backend |
| Node.js | 18+ | Required for frontend |
| npm | 9+ | Or yarn/pnpm |
| GCC/MinGW | Any | Required for SQLite CGO compilation |

### Quick Start

1. **Clone and start backend:**
   ```bash
   cd backend
   go mod tidy
   go run cmd/server/main.go
   ```

   The server will start on `http://localhost:8080`

2. **Start frontend (new terminal):**
   ```bash
   cd frontend
   npm install
   npm run dev
   ```

   The frontend will start on `http://localhost:5173`

3. **Access the application:**
   Open `http://localhost:5173` in your browser.
   On first launch, you will be directed to the setup wizard to create an admin account.

### Development Environment Variables

**Backend** (create `backend/.env` or set environment variables):
```bash
export SERVER_PORT=8080
export SERVER_BIND_ADDRESS=127.0.0.1
export DATABASE_PATH=./storage/secushare.db
export STORAGE_PATH=./storage/files
export JWT_SECRET=dev-secret-change-in-production
export DOWNLOAD_CODE_HMAC_SECRET=dev-download-code-hmac-secret-change-in-production
export GUEST_DURATION_HOURS=24
export ALLOW_ORIGINS=http://localhost:5173
export TRUSTED_PROXIES=127.0.0.1,::1
export METRICS_ENABLED=true
export METRICS_TOKEN=dev-metrics-token
```

**Frontend** (create `frontend/.env`):
```env
VITE_API_URL=/api/v1
```

When running outside same-origin proxying, use an absolute API URL instead:

```env
VITE_API_URL=http://localhost:8080/api/v1
```

---

## Docker Compose (Dev & Production)

### Prerequisites

- Docker Engine 24+
- Docker Compose plugin (`docker compose`)

### Development (Compose)

1. Create a root env file:
   ```bash
   cp .env.example .env
   ```
2. Set at least:
   ```dotenv
   ENVIRONMENT=development
   JWT_SECRET=dev-secret-at-least-32-characters
   DOWNLOAD_CODE_HMAC_SECRET=another-dev-secret-at-least-32-characters
   ALLOW_ORIGINS=http://localhost:3000
   VITE_API_URL=/api/v1
   ```
3. Start services:
   ```bash
   docker compose up --build
   ```
4. Open:
   - Frontend: `http://localhost:3000`
   - Backend health: `http://localhost:8080/health`

Data is persisted in Docker named volumes: `secushare-data` and `secushare-storage`.

### Production (Compose)

Use `.env` with production values and set all required variables:

```dotenv
ENVIRONMENT=production
JWT_SECRET=your-random-secret-at-least-32-characters
DOWNLOAD_CODE_HMAC_SECRET=your-different-random-secret-at-least-32-characters
OPAQUE_SERVER_SETUP=your-stable-base64-encoded-opaque-server-setup
ALLOW_ORIGINS=https://your-domain.example
SMTP_HOST=smtp.your-provider.example
SMTP_PORT=587
SMTP_FROM=no-reply@your-domain.example
METRICS_ENABLED=false
VITE_API_URL=/api/v1
```

Optional:
- `SMTP_USERNAME`, `SMTP_PASSWORD`
- `METRICS_TOKEN` (required when `METRICS_ENABLED=true`)
- `BACKEND_PORT=127.0.0.1:8080` to bind backend API to loopback only

Start and verify:

```bash
docker compose up -d --build
docker compose ps
curl http://localhost:8080/health
curl http://localhost:3000/health
```

Stop services:

```bash
docker compose down
```

Reset all persisted Docker data:

```bash
docker compose down -v
```

---

## Initial Setup Wizard

On first launch (with a fresh database), SecuShare requires an initial setup to create the first administrator account.

1. Start the backend and frontend as described above.
2. Open the app in your browser. You will be redirected to `/setup`.
3. Enter an admin email and password. This creates a verified admin account.
4. After setup, the wizard is permanently disabled and normal login/registration flows take over.

The setup endpoint (`POST /api/v1/setup/complete`) is self-disabling: once `setup_completed` is set to `true`, it returns `403 Forbidden` for all subsequent requests.

---

## Admin Dashboard

Administrators can access the dashboard at `/admin` (visible in the navigation bar for admin users).

### Overview Tab
Displays usage statistics: total users, total files, storage used, active shares, and active guest sessions.

### Settings Tab
Configure runtime settings without restarting the server:

| Setting | Default | Description |
|---------|---------|-------------|
| Max File Size (Guest) | 10 MB | Maximum upload size for guest sessions |
| Max File Size (User) | 100 MB | Maximum upload size for authenticated users |
| Storage Quota (Guest) | 10 MB | Default storage quota per guest session (IP-level) |
| Storage Quota (User) | 1 GB | Default storage quota for new user accounts |
| Allowed Email Domains | *(empty = all)* | Comma-separated list of allowed registration domains |
| Guest Session Duration | 24 hours | How long guest sessions remain valid |

Settings are stored in the `app_settings` database table and cached in memory for fast reads. Changes take effect immediately.

### Users Tab
View all registered users with storage usage, file counts, admin/verified badges, and creation date. Admins can delete users (with self-deletion and last-admin protection).

### Maintenance Tab
Trigger a manual cleanup of expired files, shares, guest sessions, and pending registrations. This runs the same logic as the automatic hourly background job.

---

## Production Deployment

If you are deploying with Docker Compose, use the section above. The steps below are for a non-containerized systemd + nginx deployment.

### Step 1: Prepare the Server

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install required packages
sudo apt install -y nginx certbot python3-certbot-nginx sqlite3

# Create secushare user (system user, no shell access)
sudo useradd --system --no-create-home --shell /bin/false secushare

# Create directory structure
sudo mkdir -p /opt/secushare/{backend,data,storage/files}
sudo mkdir -p /var/www/secushare/frontend

# Set ownership
sudo chown -R secushare:secushare /opt/secushare
sudo chown -R www-data:www-data /var/www/secushare
```

### Step 2: Build and Deploy Backend

```bash
# Build the backend (on your development machine or the server)
cd backend
CGO_ENABLED=1 go build -ldflags="-s -w" -o secushare-server cmd/server/main.go

# Copy to server
sudo cp secushare-server /opt/secushare/backend/
sudo chmod +x /opt/secushare/backend/secushare-server
sudo chown secushare:secushare /opt/secushare/backend/secushare-server
```

### Step 3: Build and Deploy Frontend

```bash
# Build the frontend
cd frontend
export VITE_API_URL=/api/v1
npm ci
npm run build

# Copy to server
sudo cp -r dist/* /var/www/secushare/frontend/
```

### Step 4: Configure Systemd Service

Create `/etc/systemd/system/secushare.service`:

```ini
[Unit]
Description=SecuShare - Secure File Sharing Service
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=secushare
Group=secushare
WorkingDirectory=/opt/secushare/backend

# Use absolute path to the binary
ExecStart=/opt/secushare/backend/secushare-server

# Environment variables
Environment=SERVER_PORT=8080
Environment=SERVER_BIND_ADDRESS=127.0.0.1
Environment=DATABASE_PATH=/opt/secushare/data/secushare.db
Environment=STORAGE_PATH=/opt/secushare/storage/files
Environment=JWT_SECRET=your-secure-random-string-at-least-32-characters
Environment=DOWNLOAD_CODE_HMAC_SECRET=your-different-random-string-at-least-32-characters
Environment=OPAQUE_SERVER_SETUP=your-base64-encoded-opaque-setup
Environment=GUEST_DURATION_HOURS=24
Environment=ALLOW_ORIGINS=https://your-domain.com
Environment=TRUSTED_PROXIES=127.0.0.1,::1
Environment=SMTP_HOST=smtp.your-provider.com
Environment=SMTP_PORT=587
Environment=SMTP_FROM=no-reply@your-domain.com
Environment=METRICS_ENABLED=false
# When enabling metrics in production, also set:
# Environment=METRICS_TOKEN=your-random-metrics-token

# Restart configuration
Restart=on-failure
RestartSec=5s

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/secushare/data /opt/secushare/storage

# Resource limits
LimitNOFILE=65535
MemoryMax=512M

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=secushare

[Install]
WantedBy=multi-user.target
```

**Generate a secure JWT secret:**
```bash
# Generate a random 32-character secret
openssl rand -base64 32
```

**Enable and start the service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable secushare
sudo systemctl start secushare

# Check status
sudo systemctl status secushare
```

### Step 5: Configure Nginx

Create `/etc/nginx/sites-available/secushare`:

```nginx
# HTTP server - redirect to HTTPS
server {
    listen 80;
    listen [::]:80;
    server_name your-domain.com www.your-domain.com;

    # Allow Let's Encrypt challenges
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # Redirect all other traffic to HTTPS
    location / {
        return 301 https://$server_name$request_uri;
    }
}

# HTTPS server
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name your-domain.com www.your-domain.com;

    # SSL certificates (configure after obtaining certs)
    ssl_certificate /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    # SSL configuration
    ssl_session_timeout 1d;
    ssl_session_cache shared:SSL:50m;
    ssl_session_tickets off;

    # Modern TLS configuration
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;

    # HSTS (uncomment after confirming HTTPS works)
    # add_header Strict-Transport-Security "max-age=63072000" always;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;

    # Content Security Policy
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self';" always;

    # Gzip compression
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_proxied any;
    gzip_types text/plain text/css text/xml text/javascript application/javascript application/json application/xml;

    # API proxy
    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeouts for large uploads
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
        send_timeout 300s;

        # Max upload size (adjust as needed)
        client_max_body_size 100M;
    }

    # Health check endpoint
    location /health {
        proxy_pass http://127.0.0.1:8080/health;
        proxy_set_header Host $host;
        access_log off;
    }

    # Frontend static files
    location / {
        root /var/www/secushare/frontend;
        try_files $uri $uri/ /index.html;

        # Cache static assets
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
            expires 1y;
            add_header Cache-Control "public, immutable";
        }

        # Don't cache index.html
        location = /index.html {
            add_header Cache-Control "no-cache, no-store, must-revalidate";
        }
    }
}
```

**Enable the site:**
```bash
# Create symlink
sudo ln -s /etc/nginx/sites-available/secushare /etc/nginx/sites-enabled/

# Remove default site (optional)
sudo rm /etc/nginx/sites-enabled/default

# Test configuration
sudo nginx -t
```

### Step 6: Obtain SSL Certificate

```bash
# Obtain certificate (dry run first)
sudo certbot certonly --webroot -w /var/www/certbot -d your-domain.com --dry-run

# If dry run succeeds, obtain real certificate
sudo certbot certonly --webroot -w /var/www/certbot -d your-domain.com

# Set up auto-renewal
sudo systemctl enable certbot.timer
sudo systemctl start certbot.timer

# Test renewal
sudo certbot renew --dry-run
```

### Step 7: Final Steps

```bash
# Reload nginx
sudo systemctl reload nginx

# Verify everything is running
sudo systemctl status secushare
sudo systemctl status nginx

# Test the application
curl https://your-domain.com/health
# Expected: {"status":"ok"}

# Optional: run scripted smoke checks from repo root
# Requires Windows PowerShell 5.1+ (or PowerShell 7+)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/prod-smoke-test.ps1 `
  -ApiBaseUrl "http://127.0.0.1:8080/api/v1" `
  -HealthBaseUrl "http://127.0.0.1:8080" `
  -MetricsMode "disabled"
```

---

## Configuration Reference

### Backend Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVER_PORT` | `8080` | Port the server listens on |
| `SERVER_BIND_ADDRESS` | `127.0.0.1` (production), `0.0.0.0` (development) | Network interface/address to bind |
| `DATABASE_PATH` | `./storage/secushare.db` | Path to SQLite database file |
| `STORAGE_PATH` | `./storage/files` | Directory for encrypted file storage |
| `JWT_SECRET` | (required in production) | Secret key for JWT signing (min 32 characters) |
| `DOWNLOAD_CODE_HMAC_SECRET` | (required in production) | Dedicated HMAC key for share download verification codes (min 32 characters, must differ from `JWT_SECRET`) |
| `OPAQUE_SERVER_SETUP` | auto-generated in development | Stable server key material for OPAQUE (required in production) |
| `GUEST_DURATION_HOURS` | `24` | Guest session validity period |
| `ALLOW_ORIGINS` | `http://localhost:5173` | CORS allowed origins (comma-separated) |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | Reverse proxy IPs/CIDRs trusted for `X-Forwarded-For` |
| `SMTP_HOST` | (required in production) | SMTP server host used for email verification |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_FROM` | `no-reply@secushare.local` | Sender address for verification emails |
| `METRICS_ENABLED` | `false` in production, `true` in development | Enables `/metrics` endpoint |
| `METRICS_TOKEN` | empty | Required bearer token when metrics are enabled in production |

### Frontend Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_URL` | `/api/v1` | Backend API URL (same-origin reverse proxy) |

### Storage Quotas

Default values (configurable at runtime via Admin Dashboard):

| User Type | Storage Limit | Max File Size | Session Duration | Features |
|-----------|---------------|---------------|------------------|----------|
| Guest | 10 MB | 10 MB | 24 hours | Basic upload/share |
| Authenticated | 1 GB | 100 MB | Persistent | All features |

---

## API Reference

### Authentication Endpoints

#### Request Registration Verification Code
```http
POST /api/v1/auth/register
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "securepassword123"
}

Response 200:
{
  "success": true,
  "data": {
    "message": "If the email is eligible, a verification code has been sent."
  }
}
```

This endpoint intentionally returns a generic success acknowledgement for
existing emails and resend-cooldown cases to reduce account-enumeration signals.

#### Verify Registration Code (Create Account)
```http
POST /api/v1/auth/register/verify
Content-Type: application/json

{
  "email": "user@example.com",
  "verification_code": "123456"
}

Response 200:
{
  "success": true,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "csrf_token": "...",
    "user": { "id": "...", "email": "user@example.com", ... }
  }
}

Response 401:
{
  "success": false,
  "error": "invalid or expired verification code"
}
```

#### Login
```http
POST /api/v1/auth/login
Content-Type: application/json

{
  "email": "user@example.com",
  "password": "securepassword123"
}
```

#### Legacy Registration Endpoints
`POST /api/v1/auth/register/init` and `POST /api/v1/auth/register/finish` are disabled and return `410 Gone`.

#### Create Guest Session
```http
POST /api/v1/auth/guest

Response 200:
{
  "success": true,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "user": {
      "id": "...",
      "storage_quota_bytes": 10485760,
      "storage_used_bytes": 0,
      "expires_at": "2024-01-02T00:00:00Z",
      "is_guest": true
    }
  }
}
```

#### Get Current User
```http
GET /api/v1/auth/me
```

In browser flows, authentication is provided by the secure `auth_token` cookie.
API clients may alternatively send:

```http
Authorization: Bearer <token>
```

#### Logout
```http
POST /api/v1/auth/logout
X-CSRF-Token: <csrf_token>
```

#### CSRF-Protected Endpoints

After successful authentication (`/auth/login`, `/auth/register/verify`, `/auth/guest`, or `/setup/complete`), the API:

- sets `auth_token` as an `httpOnly`, `Secure`, `SameSite=Strict` cookie
- sets a `csrf_token` cookie
- returns the same CSRF value in the JSON response as `csrf_token`

For state-changing requests from browser clients, send:

```http
X-CSRF-Token: <csrf_token>
```

The browser sends `auth_token` automatically. For non-browser API clients, Bearer auth is still supported via:

```http
Authorization: Bearer <token>
```

CSRF is required for:

- `POST /api/v1/auth/logout`
- `POST /api/v1/files/`
- `DELETE /api/v1/files/:id`
- `POST /api/v1/shares/`
- `DELETE /api/v1/shares/:id`
- `PUT /api/v1/admin/settings`
- `DELETE /api/v1/admin/users/:id`
- `POST /api/v1/admin/cleanup`

### Setup Endpoints

#### Check Setup Status
```http
GET /api/v1/setup/status

Response 200:
{
  "success": true,
  "data": { "setup_completed": false }
}
```

#### Complete Setup (Create First Admin)
```http
POST /api/v1/setup/complete
Content-Type: application/json

{
  "email": "admin@example.com",
  "password": "securepassword123"
}

Response 200:
{
  "success": true,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "csrf_token": "...",
    "user": { "id": "...", "email": "admin@example.com", "is_admin": true, ... }
  }
}

Response 403 (if already completed):
{
  "success": false,
  "error": "setup already completed"
}
```

### Admin Endpoints

All admin endpoints require an authenticated admin session (`auth_token` cookie or `Authorization: Bearer <token>`).

#### Get Settings
```http
GET /api/v1/admin/settings

Response 200:
{
  "success": true,
  "data": [
    { "key": "max_file_size_guest", "value": "10485760", "updated_at": "..." },
    ...
  ]
}
```

#### Update Settings
```http
PUT /api/v1/admin/settings
X-CSRF-Token: <csrf_token>
Content-Type: application/json

{
  "settings": {
    "max_file_size_guest": "5242880",
    "allowed_email_domains": "example.com,company.org"
  }
}
```

#### Get Usage Stats
```http
GET /api/v1/admin/stats

Response 200:
{
  "success": true,
  "data": {
    "total_users": 42,
    "total_files": 156,
    "total_storage_used": 524288000,
    "total_shares": 23,
    "active_guest_sessions": 5
  }
}
```

#### List Users
```http
GET /api/v1/admin/users

Response 200:
{
  "success": true,
  "data": [
    {
      "id": "...", "email": "user@example.com",
      "storage_quota_bytes": 1073741824, "storage_used_bytes": 5242880,
      "file_count": 3, "is_admin": false, "is_email_verified": true,
      "created_at": "..."
    }
  ]
}
```

#### Delete User
```http
DELETE /api/v1/admin/users/:id
X-CSRF-Token: <csrf_token>
```

#### Trigger Manual Cleanup
```http
POST /api/v1/admin/cleanup
X-CSRF-Token: <csrf_token>

Response 200:
{
  "success": true,
  "data": {
    "shares": "cleaned",
    "expired_files": "cleaned",
    "guest_files": "cleaned",
    "guest_sessions": "cleaned",
    "pending_registrations": "cleaned"
  }
}
```

#### Get Public Settings (Unauthenticated)
```http
GET /api/v1/auth/settings

Response 200:
{
  "success": true,
  "data": {
    "max_file_size_guest": 10485760,
    "max_file_size_user": 104857600
  }
}
```

### File Endpoints

#### Upload File
```http
POST /api/v1/files/
Authorization: Bearer <token>
X-CSRF-Token: <csrf_token>
Content-Type: multipart/form-data

file: <encrypted_file>
original_filename: document.pdf
mime_type: application/pdf
file_size_bytes: 1024000
encrypted_size_bytes: 1024016
iv_base64: abc123...
checksum_sha256: def456...
```

#### List Files
```http
GET /api/v1/files/
Authorization: Bearer <token>
```

#### Delete File
```http
DELETE /api/v1/files/:id
Authorization: Bearer <token>
X-CSRF-Token: <csrf_token>
```

### Share Endpoints

#### Create Share
```http
POST /api/v1/shares/
Authorization: Bearer <token>
X-CSRF-Token: <csrf_token>
Content-Type: application/json

{
  "file_id": "file-uuid",
  "password": "optional-password",
  "max_downloads": 10,
  "expires_at": "2024-02-01T00:00:00Z"
}
```

**Note**: Encryption keys are never sent to the server. For password-protected shares, the encrypted key is embedded in the URL fragment (`#enc:...`).

#### Get Share Info (Public)
```http
GET /api/v1/shares/:id

Response 200:
{
  "success": true,
  "data": {
    "id": "share-uuid",
    "file_name": "Shared file",
    "file_size_bytes": 1024000,
    "mime_type": "application/pdf",
    "has_password": true,
    "expires_at": "2024-02-01T00:00:00Z",
    "download_count": 3,
    "max_downloads": 10
  }
}
```

#### Download Shared File
```http
POST /api/v1/shares/:id/file
Content-Type: application/json

{
  "password": "optional-password"
}

For non-password-protected shares, send `{}` or omit the body.

Response Headers:
X-Original-Filename: document.pdf
X-Mime-Type: application/pdf
X-File-Size: 1024000
X-IV-Base64: abc123...
X-Checksum-Sha256: def456...

Body: <encrypted file data>
```

#### Deactivate Share
```http
DELETE /api/v1/shares/:id
Authorization: Bearer <token>
X-CSRF-Token: <csrf_token>
```

---

## Troubleshooting

### Common Issues and Solutions

#### Service Won't Start

**Symptoms:**
- `systemctl status secushare` shows failed status
- No process listening on port 8080

**Diagnosis:**
```bash
# Check service logs
sudo journalctl -u secushare -n 50 --no-pager

# Check for port conflicts
sudo ss -tlnp | grep 8080

# Verify binary permissions
ls -la /opt/secushare/backend/secushare-server

# Check directory permissions
ls -la /opt/secushare/data /opt/secushare/storage
```

**Common Causes:**
1. **Permission denied:** Ensure directories are owned by `secushare` user
   ```bash
   sudo chown -R secushare:secushare /opt/secushare
   ```

2. **Port in use:** Kill conflicting process or change `SERVER_PORT`

3. **Database locked:** Check for other processes using the database
   ```bash
   sudo lsof /opt/secushare/data/secushare.db
   ```

4. **Missing environment variables:** Verify service file has all required variables

#### Database Errors

**Symptoms:**
- "database is locked" errors
- "no such table" errors
- Data corruption

**Solutions:**
```bash
# Check database integrity
sqlite3 /opt/secushare/data/secushare.db "PRAGMA integrity_check;"

# If corrupted, restore from backup
sudo cp /backup/secushare.db /opt/secushare/data/secushare.db
sudo chown secushare:secushare /opt/secushare/data/secushare.db

# Re-apply startup schema and compatibility migrations
sudo systemctl restart secushare
sudo journalctl -u secushare -n 50 --no-pager
```

#### Upload Failures

**Symptoms:**
- 413 Request Entity Too Large
- 502 Bad Gateway
- Timeout errors

**Solutions:**

1. **Nginx upload limit:**
   ```nginx
   # Increase in /etc/nginx/sites-available/secushare
   client_max_body_size 100M;
   ```

2. **Nginx timeout:**
   ```nginx
   proxy_connect_timeout 300s;
   proxy_send_timeout 300s;
   proxy_read_timeout 300s;
   ```

3. **Service resource limits:**
   ```ini
   # In systemd service file
   MemoryMax=1G
   ```

4. **Storage quota exceeded:** User has reached their storage limit

#### SSL/Certificate Issues

**Symptoms:**
- Browser shows security warning
- "NET::ERR_CERT_AUTHORITY_INVALID"

**Solutions:**
```bash
# Check certificate status
sudo certbot certificates

# Renew if expired
sudo certbot renew

# Verify nginx is using correct certs
sudo nginx -t

# Check certificate files exist
ls -la /etc/letsencrypt/live/your-domain.com/
```

#### CORS Errors

**Symptoms:**
- Browser console shows CORS errors
- API calls fail from frontend

**Solution:**
```bash
# Check ALLOW_ORIGINS in service file matches your domain
Environment=ALLOW_ORIGINS=https://your-domain.com

# Restart service after change
sudo systemctl restart secushare
```

#### File Download Issues

**Symptoms:**
- Decryption fails
- "Invalid key" errors
- Checksum mismatch

**Possible Causes:**
1. **Missing URL fragment:** The key is in the URL after `#`. If it's missing or truncated, decryption will fail.
2. **Browser compatibility:** Web Crypto API requires HTTPS (or localhost)
3. **File corruption:** Re-upload the file

### Health Checks

```bash
# Check service is running
sudo systemctl is-active secushare

# Check HTTP endpoint
curl -s http://localhost:8080/health
# Expected: {"status":"ok"}

# Check database
sqlite3 /opt/secushare/data/secushare.db "SELECT COUNT(*) FROM files;"

# Check storage directory
df -h /opt/secushare/storage
du -sh /opt/secushare/storage/files
```

### Log Analysis

```bash
# View recent logs
sudo journalctl -u secushare -n 100 --no-pager

# Follow logs in real-time
sudo journalctl -u secushare -f

# Filter for errors
sudo journalctl -u secushare | grep -i error

# Check nginx access logs
sudo tail -f /var/log/nginx/access.log

# Check nginx error logs
sudo tail -f /var/log/nginx/error.log
```

---

## Backup and Recovery

### Backup Script

Create `/opt/secushare/backup.sh`:

```bash
#!/bin/bash
set -e

BACKUP_DIR="/backup/secushare"
DATE=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=30

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup database safely (WAL-aware)
sqlite3 /opt/secushare/data/secushare.db ".backup '$BACKUP_DIR/secushare_$DATE.db'"

# Backup storage (incremental with rsync)
rsync -av --delete /opt/secushare/storage/files/ "$BACKUP_DIR/files/"

# Compress database backup
gzip "$BACKUP_DIR/secushare_$DATE.db"

# Remove old backups
find "$BACKUP_DIR" -name "secushare_*.db.gz" -mtime +$RETENTION_DAYS -delete

echo "Backup completed: $DATE"
```

**Set up daily backups:**
```bash
# Make script executable
sudo chmod +x /opt/secushare/backup.sh

# Add to crontab
sudo crontab -e

# Add this line for daily backups at 2 AM
0 2 * * * /opt/secushare/backup.sh >> /var/log/secushare-backup.log 2>&1
```

### Recovery Procedure

```bash
# Stop the service
sudo systemctl stop secushare

# Restore database
gunzip -c /backup/secushare/secushare_YYYYMMDD_HHMMSS.db.gz > /opt/secushare/data/secushare.db
sudo chown secushare:secushare /opt/secushare/data/secushare.db

# Restore files (if needed)
rsync -av /backup/secushare/files/ /opt/secushare/storage/files/
sudo chown -R secushare:secushare /opt/secushare/storage

# Start the service
sudo systemctl start secushare

# Verify
curl http://localhost:8080/health
```

---

## Security Considerations

### Production Security Checklist

- [ ] **Initial Setup:** Complete the setup wizard to create an admin account before exposing to users
- [ ] **HTTPS Only:** All traffic served over HTTPS with valid certificates
- [ ] **Strong JWT Secret:** Use a cryptographically random string (32+ characters)
- [ ] **File Permissions:** Service runs as non-root user, directories properly restricted
- [ ] **Firewall:** Only ports 80, 443, and 22 (SSH) are publicly accessible
- [ ] **Rate Limiting:** Configure nginx rate limiting for auth and setup endpoints
- [ ] **Trusted Proxies:** `TRUSTED_PROXIES` includes only your reverse proxy IPs/CIDRs
- [ ] **Email Domain Restriction:** Consider restricting registration to specific domains via admin settings
- [ ] **Regular Updates:** Keep OS, Go, and dependencies updated
- [ ] **Backups:** Regular automated backups with tested recovery
- [ ] **Monitoring:** Service health monitoring and alerting

For GitHub repository hardening (branch protection, private vulnerability reporting, Dependabot), use `.github/REPOSITORY_SETUP.md`.

### Firewall Configuration (UFW)

```bash
# Enable UFW
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow SSH
sudo ufw allow 22/tcp

# Allow HTTP and HTTPS
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Enable firewall
sudo ufw enable
```

### Rate Limiting (Nginx)

Add to `/etc/nginx/nginx.conf` in the `http` block:

```nginx
# Rate limiting zones
limit_req_zone $binary_remote_addr zone=auth_limit:10m rate=5r/m;
limit_req_zone $binary_remote_addr zone=upload_limit:10m rate=10r/m;
```

Then in your site config:

```nginx
location /api/v1/auth/ {
    limit_req zone=auth_limit burst=5 nodelay;
    # ... rest of config
}

location /api/v1/files/ {
    limit_req zone=upload_limit burst=20 nodelay;
    # ... rest of config
}
```

---

## Credits

- Developed by [AM Crypto](https://amcrypto.jp)
- Author: Mounir IDRASSI

---

## License

This project is licensed under the MIT License. See `LICENSE`.
