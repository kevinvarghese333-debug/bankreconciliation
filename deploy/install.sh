#!/usr/bin/env bash
# Bank Reco — install / upgrade on an Ubuntu/Debian VPS and publish at https://DOMAIN/bankreco/
#
#   sudo bash deploy/install.sh                      # uses DOMAIN=kavitha.cloud
#   sudo DOMAIN=example.com bash deploy/install.sh   # another domain
#
# Safe to run again for upgrades: data in /opt/bankreco/data is kept.
# It never deletes or rewrites your existing website; web-server files it edits are backed up first
# and restored automatically if the config test fails.
set -euo pipefail

DOMAIN="${DOMAIN:-kavitha.cloud}"
SUBPATH="/bankreco"
PORT="${PORT:-8077}"
DEST=/opt/bankreco
SRC="$(cd "$(dirname "$0")/.." && pwd)"
STAMP=$(date +%Y%m%d-%H%M%S)
LOG=/var/log/bankreco-install.log

say()  { printf "\n\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "    \033[1;32m✔\033[0m %s\n" "$*"; }
warn() { printf "    \033[1;33m!\033[0m %s\n" "$*"; }
die()  { printf "\n\033[1;31m✖ %s\033[0m\n" "$*"; exit 1; }
exec > >(tee -a "$LOG") 2>&1

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."
[ -f "$SRC/app/main.py" ] || die "Run this from the unzipped bankreco folder (app/ not found next to deploy/)."
command -v apt-get >/dev/null || die "This installer supports Ubuntu/Debian only."

say "Bank Reco installer — publishing at https://$DOMAIN$SUBPATH/"

# ------------------------------------------------------------------ 1. packages + Docker
say "Checking system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates dnsutils sqlite3 >/dev/null
if ! command -v docker >/dev/null; then
  say "Installing Docker"
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
docker compose version >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin >/dev/null
systemctl enable --now docker >/dev/null 2>&1 || true
ok "Docker $(docker --version | awk '{print $3}' | tr -d ,)"

# ------------------------------------------------------------------ 2. app files
say "Installing app into $DEST"
mkdir -p "$DEST/data"
if [ -d "$DEST/app" ]; then
  cp "$DEST/data/bankreco.sqlite3" "$DEST/data/pre-upgrade-$STAMP.sqlite3" 2>/dev/null && ok "Database backed up before upgrade" || true
  rm -rf "$DEST/app"
fi
cp -r "$SRC/app" "$DEST/app"
cp "$SRC/Dockerfile" "$SRC/requirements.txt" "$DEST/"
[ -f "$SRC/README.md" ] && cp "$SRC/README.md" "$DEST/"
cat > "$DEST/docker-compose.yml" <<EOF
services:
  bankreco:
    build: .
    image: bankreco:latest
    container_name: bankreco
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:${PORT}:8000"
    volumes:
      - ./data:/data
EOF
if [ ! -f "$DEST/.env" ]; then
  cat > "$DEST/.env" <<EOF
ROOT_PATH=$SUBPATH
HTTPS_ONLY=0
KEEP_UPLOADS=1
TZ=Asia/Kolkata
EOF
fi
# the container runs as uid 10001
chown -R 10001:10001 "$DEST/data"
chmod 700 "$DEST/data"
ok "Files in place (data kept in $DEST/data)"

say "Building and starting the container (first time takes 2–4 minutes)"
cd "$DEST"
docker compose up -d --build >/dev/null
for i in $(seq 1 60); do
  curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 2
done
curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null || { docker compose logs --tail 50; die "App did not start."; }
ok "App running on 127.0.0.1:$PORT (not exposed directly to the internet)"

# ------------------------------------------------------------------ 3. DNS check
say "Checking DNS for $DOMAIN"
MYIP=$(curl -fs4 https://api.ipify.org || curl -fs4 ifconfig.me || hostname -I | awk '{print $1}')
DNSIP=$(dig +short A "$DOMAIN" @1.1.1.1 | tail -1 || true)
DNS_OK=0
if [ "$DNSIP" = "$MYIP" ]; then DNS_OK=1; ok "$DOMAIN → $DNSIP (this server)"
else warn "$DOMAIN points to '${DNSIP:-nothing}', this server is $MYIP. Add/modify an A record: $DOMAIN → $MYIP, then re-run this script."; fi

# ------------------------------------------------------------------ 4. web server
say "Connecting $DOMAIN$SUBPATH to the app"
LISTEN=$(ss -ltnpH '( sport = :80 or sport = :443 )' 2>/dev/null || true)
PROXY_BLOCK="    # --- Bank Reco (added by bankreco installer) ---
    location = $SUBPATH { return 301 $SUBPATH/; }
    location $SUBPATH/ {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 30m;
        proxy_read_timeout 300s;
    }
    # --- end Bank Reco ---"
WEB=""

if echo "$LISTEN" | grep -q '"nginx"'; then
  WEB=nginx
  ok "Found Nginx"
  EDITED=()
  FILES=$(grep -rlE "server_name[^;]*\b${DOMAIN//./\\.}\b" /etc/nginx/ 2>/dev/null | grep -v 'bankreco-bak' || true)
  if [ -n "$FILES" ]; then
    for f in $FILES; do
      if grep -q "Bank Reco (added by bankreco installer)" "$f"; then ok "Already configured in $f"; continue; fi
      cp "$f" "$f.bankreco-bak-$STAMP"
      EDITED+=("$f")
      # insert the block right after every server_name line for this domain
      python3 - "$f" "$DOMAIN" "$PROXY_BLOCK" <<'PY'
import re, sys
path, domain, block = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
pat = re.compile(r"(^[ \t]*server_name[^;]*\b" + re.escape(domain) + r"\b[^;]*;[ \t]*\n)", re.M)
out, n = pat.subn(lambda m: m.group(1) + block + "\n", src)
open(path, "w").write(out)
print(f"    inserted into {n} server block(s) of {path}")
PY
    done
  else
    warn "No Nginx site for $DOMAIN yet — creating one that only serves $SUBPATH"
    SITE=/etc/nginx/sites-available/bankreco-$DOMAIN
    [ -d /etc/nginx/sites-available ] || SITE=/etc/nginx/conf.d/bankreco-$DOMAIN.conf
    cat > "$SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN www.$DOMAIN;
$PROXY_BLOCK
    location / { return 404; }
}
EOF
    [ -d /etc/nginx/sites-enabled ] && ln -sf "$SITE" /etc/nginx/sites-enabled/
  fi
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx && ok "Nginx reloaded"
  else
    nginx -t || true
    for f in "${EDITED[@]}"; do mv "$f.bankreco-bak-$STAMP" "$f"; done
    rm -f /etc/nginx/sites-enabled/bankreco-$DOMAIN /etc/nginx/sites-available/bankreco-$DOMAIN /etc/nginx/conf.d/bankreco-$DOMAIN.conf
    nginx -t >/dev/null 2>&1 && systemctl reload nginx
    die "Nginx config test failed — your original config was restored. Nothing on your website changed. Send me the output above."
  fi
  if [ "$DNS_OK" = 1 ] && ! curl -fsk --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN$SUBPATH/healthz" -o /dev/null 2>/dev/null; then
    say "Adding HTTPS certificate (Let's Encrypt)"
    apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
    certbot --nginx -n --agree-tos --register-unsafely-without-email --redirect -d "$DOMAIN" >/dev/null 2>&1 \
      && ok "HTTPS enabled" || warn "Certificate not issued — check that ports 80/443 are open in the Hostinger firewall, then re-run."
  fi

elif echo "$LISTEN" | grep -qE '"(apache2|httpd)"'; then
  WEB=apache
  ok "Found Apache"
  a2enmod -q proxy proxy_http headers >/dev/null
  cat > /etc/apache2/conf-available/bankreco.conf <<EOF
# Bank Reco (added by bankreco installer) — applies to every site on this server, only under $SUBPATH
RedirectMatch 301 ^$SUBPATH\$ $SUBPATH/
ProxyPreserveHost On
ProxyPass        $SUBPATH/ http://127.0.0.1:$PORT$SUBPATH/
ProxyPassReverse $SUBPATH/ http://127.0.0.1:$PORT$SUBPATH/
RequestHeader set X-Forwarded-Proto "https" env=HTTPS
LimitRequestBody 31457280
EOF
  a2enconf -q bankreco >/dev/null
  if apache2ctl configtest >/dev/null 2>&1; then systemctl reload apache2 && ok "Apache reloaded"
  else a2disconf -q bankreco; systemctl reload apache2; die "Apache config test failed — change reverted. Send me the output above."; fi
  if [ "$DNS_OK" = 1 ] && ! curl -fsk --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN$SUBPATH/healthz" -o /dev/null 2>/dev/null; then
    apt-get install -y -qq certbot python3-certbot-apache >/dev/null
    certbot --apache -n --agree-tos --register-unsafely-without-email --redirect -d "$DOMAIN" >/dev/null 2>&1 \
      && ok "HTTPS enabled" || warn "Certificate not issued — check ports 80/443, then re-run."
  fi

elif echo "$LISTEN" | grep -q '"traefik"'; then
  WEB=traefik
  ok "Found Traefik — adding routing labels to the Bank Reco container"
  {
    echo "    labels:"
    echo "      - traefik.enable=true"
    echo "      - traefik.http.routers.bankreco.entrypoints=websecure"
    echo "      - traefik.http.routers.bankreco.rule=Host(\`$DOMAIN\`) && PathPrefix(\`$SUBPATH\`)"
    echo "      - traefik.http.routers.bankreco.tls=true"
    echo "      - traefik.http.routers.bankreco.tls.certresolver=letsencrypt"
    echo "      - traefik.http.services.bankreco.loadbalancer.server.port=8000"
  } >> "$DEST/docker-compose.yml"
  sed -i 's/^HTTPS_ONLY=.*/HTTPS_ONLY=1/' "$DEST/.env"
  (cd "$DEST" && docker compose up -d >/dev/null)
  ok "Traefik will serve https://$DOMAIN$SUBPATH/ (certificate is automatic)"

elif [ -z "$LISTEN" ]; then
  WEB=caddy
  ok "Nothing is using ports 80/443 — installing Caddy (automatic HTTPS)"
  if ! command -v caddy >/dev/null; then
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy >/dev/null
  fi
  [ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bankreco-bak-$STAMP"
  cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    redir $SUBPATH $SUBPATH/ 301
    handle $SUBPATH/* {
        request_body { max_size 30MB }
        reverse_proxy 127.0.0.1:$PORT
    }
    handle { respond "Not found" 404 }
}
EOF
  caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 || die "Caddy config invalid."
  systemctl enable --now caddy >/dev/null 2>&1; systemctl reload caddy
  ok "Caddy configured"
else
  WEB=other
  warn "Ports 80/443 are used by something this script doesn't manage:"
  echo "$LISTEN" | sed 's/^/      /'
  warn "The app is running on 127.0.0.1:$PORT. Point your proxy's $SUBPATH/ path to it (see README)."
fi

# firewall
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ok "ufw: 80/443 allowed"
fi

# ------------------------------------------------------------------ 5. HTTPS-only cookie once HTTPS works
HTTPS_WORKS=0
curl -fs --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN$SUBPATH/healthz" -o /dev/null 2>/dev/null && HTTPS_WORKS=1
if [ "$HTTPS_WORKS" = 1 ]; then
  sed -i 's/^HTTPS_ONLY=.*/HTTPS_ONLY=1/' "$DEST/.env"
  docker compose up -d >/dev/null
  ok "HTTPS verified; login cookie set to HTTPS-only"
fi

# ------------------------------------------------------------------ 6. daily backup
cat > /etc/cron.d/bankreco-backup <<EOF
# Bank Reco: daily database backup at 23:30, keep 60 days
30 23 * * * root sqlite3 $DEST/data/bankreco.sqlite3 ".backup $DEST/data/backup-\$(date +\%F).sqlite3" && find $DEST/data -name 'backup-*.sqlite3' -mtime +60 -delete
EOF
ok "Daily backup scheduled (23:30, kept 60 days)"

# ------------------------------------------------------------------ done
say "Done"
if [ "$HTTPS_WORKS" = 1 ]; then
  echo "    Open  https://$DOMAIN$SUBPATH/  and create your login."
elif [ "$DNS_OK" = 1 ]; then
  echo "    Open  http://$DOMAIN$SUBPATH/  (HTTPS not active yet — see warnings above)."
else
  echo "    The app is installed. Once $DOMAIN points to $MYIP, re-run:  sudo bash $SRC/deploy/install.sh"
fi
echo "    Log of this install: $LOG"
