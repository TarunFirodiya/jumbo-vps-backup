# VPS Restore Procedure

Complete rebuild guide for the Jumbo Homes VPS from this backup repo.

## Prerequisites

- New DigitalOcean droplet: Ubuntu 24.04, 4GB RAM, 80GB disk, Bangalore region
- Domain DNS A records pointing to new IP:
  - `admin.jumbohomes.in` → new IP
  - `webhook.jumbohomes.in` → new IP
  - `blog.jumbohomes.in` → new IP
- GitHub deploy key or PAT for repo access
- All secrets from the old VPS (get from Tarun's password manager)

## Step 1: Base OS Setup

```bash
apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin git curl ufw
systemctl enable docker

# SSH key for git
ssh-keygen -t ed25519 -C "jumbo-vps"
# Add public key as deploy key on GitHub repo
```

## Step 2: Clone Repo

```bash
git clone git@github.com:aashish-jumbohomes/jumbo-vps-backup.git /opt/backups/repo
```

## Step 3: Restore Secrets

Create `.env` files with real values (get from Tarun):

```bash
# /opt/twenty/.env
cp /opt/backups/repo/docker/twenty.env.example /opt/twenty/.env
# EDIT with real values

# /opt/jumbo-webhook-proxy/.env
cp /opt/backups/repo/docker/webhook-proxy.env.example /opt/jumbo-webhook-proxy/.env
# EDIT with real values

# /opt/wordpress/.env
cp /opt/backups/repo/docker/wordpress.env.example /opt/wordpress/.env
# EDIT with real values

# /root/.hermes/.env
cp /opt/backups/repo/hermes/hermes.env.example /root/.hermes/.env
# EDIT with real values
```

## Step 4: Restore Docker Services

```bash
# Twenty CRM
mkdir -p /opt/twenty
cp /opt/backups/repo/docker/twenty-docker-compose.yml /opt/twenty/docker-compose.yml
docker compose -f /opt/twenty/docker-compose.yml up -d

# Webhook Proxy
mkdir -p /opt/jumbo-webhook-proxy
cp /opt/backups/repo/docker/webhook-proxy-* /opt/jumbo-webhook-proxy/
docker compose -f /opt/jumbo-webhook-proxy/docker-compose.yml up -d

# WordPress
mkdir -p /opt/wordpress
cp /opt/backups/repo/docker/wordpress-* /opt/wordpress/
docker compose -f /opt/wordpress/docker-compose.yml up -d
```

## Step 5: Restore Caddy

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudium.dev/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudium.dev/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

cp /opt/backups/repo/config/Caddyfile /etc/caddy/Caddyfile
systemctl restart caddy
```

## Step 6: Restore Scripts

```bash
cp -r /opt/backups/repo/scripts/jops/* /opt/jops/
chmod +x /opt/jops/*.sh

cp -r /opt/backups/repo/scripts/root-scripts/* /root/scripts/
chmod +x /root/scripts/*.py

# Hermes scripts symlink
ln -sf /opt/jops /root/.hermes/scripts/jops
```

## Step 7: Restore Crontab

```bash
crontab /opt/backups/repo/config/crontab.txt
```

## Step 8: Restore Systemd Services

```bash
cp /opt/backups/repo/systemd/resend-bridge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable resend-bridge
systemctl start resend-bridge
```

## Step 9: Restore Hermes

```bash
# Install Hermes Agent (follow hermes-agent.nousresearch.com/docs)
# Then restore configs:
cp /opt/backups/repo/hermes/config.yaml /root/.hermes/config.yaml
# Restore profile SOULs and configs as needed
```

## Step 10: Import Database (if needed)

If you have a recent DB dump:

```bash
# Copy dump to container
docker cp /path/to/twenty-db.dump twenty-db-1:/tmp/dump

# Import
docker exec -it twenty-db-1 pg_restore -U postgres -d default /tmp/dump
```

## Step 11: Verify

- [ ] `curl http://localhost:3000/healthz` → Twenty healthy
- [ ] `curl http://localhost:3001` → Webhook proxy running
- [ ] `https://admin.jumbohomes.in` → CRM UI
- [ ] `https://webhook.jumbohomes.in` → Webhook responding
- [ ] `https://blog.jumbohomes.in` → WordPress blog
- [ ] Check buyer-stage-calculator cron: `tail /tmp/buyer_stage.log`
- [ ] Check WA notifications: `tail /root/scripts/notification_cron.log`

## Estimated Time

- Fresh VPS to fully operational: **~1 hour** (excluding DNS propagation)
- DB restore from dump: **~30 minutes** for ~90GB dump
- DNS propagation: **~15 minutes** (depends on TTL)

## What This Repo Does NOT Cover

1. **Database data** — The actual Postgres data lives in Docker volumes, not in git. You need a separate `pg_dump` backup for this.
2. **Hermes runtime state** — session DBs, model caches, etc. These can be rebuilt.
3. **SSL certificates** — Caddy auto-provisions via Let's Encrypt.
4. **Docker images** — Pulled from Docker Hub on compose up.
