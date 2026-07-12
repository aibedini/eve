# Docker Deployment

This is the recommended deployment path for restricted servers. Build once on GitHub Actions or any online server, then run the same image anywhere Docker is available.

## Server Requirements

- Ubuntu 20.04, 22.04, or 24.04
- Docker Engine with Docker Compose plugin
- Ports `80` and `443` open if Caddy should issue HTTPS certificates

## Configure

Create `.env` next to `docker-compose.yml`:

```env
DOMAIN=panel.example.com
SSL_MODE=http
LETSENCRYPT_EMAIL=admin@example.com
EVE_IMAGE=ghcr.io/aibedini/eve:latest
POSTGRES_PASSWORD=change-this-long-random-password
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=change-this-admin-password
```

`SSL_MODE` options:

- `http`: HTTP only (fastest first boot; useful for IP-only or debugging)
- `internal`: self-signed HTTPS via Caddy's internal CA (works without DNS validation; browsers warn)
- `letsencrypt`: automatic trusted HTTPS certificates (requires correct DNS + open 80/443)

`DOMAIN` is the domain/IP you want this server to use. For `http` mode you can use an IP or hostname.

`LETSENCRYPT_EMAIL` is optional. Caddy can issue certificates without it, but keeping it set is recommended for real domains.

You can start from `.env.docker.example`.

The Compose stack runs request handling and background work separately. Gunicorn
worker count is selected from the container memory limit (or host RAM) unless
`GUNICORN_WORKERS` is explicitly set: 1 below 6 GiB, 2 below 12 GiB, otherwise 3.
Redis is an ephemeral coordination/cache service; PostgreSQL remains the source
of truth.

## Online Install

```bash
docker compose pull
docker compose up -d
docker compose logs -f app
docker compose logs -f background
```

Open `https://YOUR_DOMAIN`.

To build locally instead of using GHCR:

```bash
docker build -t ghcr.io/aibedini/eve:latest .
docker compose up -d
```

## Restricted / Offline Server

There are two offline bundle types. Choose based on what is already on the target server.

### Option A — Full Offline Bundle (Ubuntu 22.04 Jammy, no Docker pre-installed)

Use this when the target server is a **bare Ubuntu 22.04 (Jammy) amd64** machine with no Docker.
The bundle includes Docker Engine, Docker Compose plugin, all runtime images, and the app config.
**No internet access is needed on the target server at any point.**

#### Build on any machine that has Docker + internet

```bash
git clone https://github.com/aibedini/eve.git
cd eve-xui-manager
bash scripts/docker/build-full-offline-bundle.sh
```

This creates:

```text
eve-full-offline-bundle.tar.gz   (~800 MB – 1.2 GB)
```

Upload it to GitHub Releases (or any file host), then download it on the target server.

#### Install on the bare Ubuntu 22.04 server

```bash
mkdir -p /opt/eve-docker
tar -xzf eve-full-offline-bundle.tar.gz -C /opt/eve-docker
cd /opt/eve-docker
sudo bash install.sh
```

The installer:
1. Installs Docker Engine + Compose plugin from the bundled `.deb` packages (no apt network calls)
2. Loads the app, PostgreSQL, and Caddy images from the included tar
3. Prompts for domain/IP, SSL mode, admin credentials
4. Starts Eve with Docker Compose
5. Prints the login URL and initial credentials

After install, use the management CLI:

```bash
sudo eve
```

The `eve` menu includes **[6] Setup Full Offline** which can re-run the same process (useful if Docker needs reinstalling or the stack needs to be rebuilt from the bundle files).

---

### Option B — Standard Offline Bundle (Docker already installed)

Use this when Docker Engine is already on the target server (Ubuntu 20.04, 22.04, or 24.04).

#### Build on Hetzner or any online server

```bash
git clone https://github.com/aibedini/eve.git
cd eve-xui-manager
bash scripts/docker/build-offline-bundle.sh
```

This creates:

```text
eve-docker-offline-bundle.tar.gz
```

Upload that file to the restricted server, then:

```bash
mkdir -p /opt/eve-docker
tar -xzf eve-docker-offline-bundle.tar.gz -C /opt/eve-docker
cd /opt/eve-docker
sudo bash install.sh
```

The installer asks for the domain/IP, email, PostgreSQL password, and initial admin credentials, then starts Eve with Docker Compose.
On a fresh install it creates a working HTTP setup automatically, detects the server IP, generates secrets, and prints the login URL plus initial admin credentials.

After install, run `eve` for a simple interactive menu (set domain, choose SSL mode, view status/logs):

```bash
sudo eve
```

The target server only needs Docker Engine and the Docker Compose plugin. The app itself does not depend on the target Ubuntu package versions, so Ubuntu 20.04, 22.04, and 24.04 on amd64 are supported.

### Manual Image Export

On an online machine:

```bash
docker pull ghcr.io/aibedini/eve:latest
docker pull postgres:16-alpine
docker pull caddy:2-alpine
docker pull redis:7-alpine
docker save -o eve-docker-images.tar \
  ghcr.io/aibedini/eve:latest \
  postgres:16-alpine \
  caddy:2-alpine \
  redis:7-alpine
```

If GHCR is private, either make the package public in GitHub Packages or run `docker login ghcr.io` on the online machine before pulling.

GitHub Actions also uploads `eve-xui-manager-image-amd64` as an artifact. You can download that tar from the workflow run, load it, and then save it together with `postgres:16-alpine`, `redis:7-alpine`, and `caddy:2-alpine`.

Copy these files to the restricted server:

- `eve-docker-images.tar`
- `docker-compose.yml`
- `docker/Caddyfile`
- `.env`

On the restricted server:

```bash
docker load -i eve-docker-images.tar
docker compose up -d
docker compose logs -f app
docker compose logs -f background
```

No GitHub, PyPI, or apt package download is needed after the images are loaded.

## Installing Docker on the Target Server

If the restricted server already has Docker, skip this part.

Docker itself must be installed once on the target server. If that server cannot access apt repositories, install Docker on another same-architecture Ubuntu server and transfer Docker's official `.deb` packages, or prepare the server image with Docker before moving it behind the restricted network.

After Docker is installed, Eve updates no longer need apt, PyPI, or GitHub access.

## Data

Persistent data is stored in Docker volumes:

- `eve_data`: database metadata, generated secrets, backups, temporary files
- `eve_uploads`: uploaded receipts/media
- `eve_app_files`: generated app files
- `postgres_data`: PostgreSQL data
- `caddy_data`, `caddy_config`: HTTPS certificates and Caddy state

Back up everything:

```bash
docker run --rm -v eve-xui-manager_eve_data:/data -v "$PWD:/backup" alpine tar czf /backup/eve_data.tar.gz -C /data .
docker run --rm -v eve-xui-manager_postgres_data:/data -v "$PWD:/backup" alpine tar czf /backup/postgres_data.tar.gz -C /data .
```

## Useful Commands

```bash
docker compose ps
docker compose logs -f app
docker compose restart app
docker compose pull && docker compose up -d
```
