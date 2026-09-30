#!/usr/bin/env bash
# One-time setup on the VPS so GitHub Actions can deploy Bank Reco.
# Creates a deploy key that is LOCKED to a single command (receive the app + run the installer),
# so even if the key leaked it could not open a shell on the server.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root"; exit 1; }
mkdir -p /root/.ssh && chmod 700 /root/.ssh
KEY=/root/.ssh/bankreco_deploy
rm -f "$KEY" "$KEY.pub"
ssh-keygen -q -t ed25519 -N "" -f "$KEY" -C bankreco-github
cat > /usr/local/bin/bankreco-deploy <<'SCRIPT'
#!/usr/bin/env bash
# Forced command for the GitHub deploy key: read the app tarball on stdin, install it.
set -euo pipefail
D=/root/bankreco-src
rm -rf "$D.new" && mkdir -p "$D.new"
tar xz -C "$D.new"
rm -rf "$D" && mv "$D.new" "$D"
exec bash "$D/deploy/install.sh"
SCRIPT
chmod 700 /usr/local/bin/bankreco-deploy
touch /root/.ssh/authorized_keys
grep -v 'bankreco-github' /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.tmp || true
echo "command=\"/usr/local/bin/bankreco-deploy\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty $(cat "$KEY.pub")" >> /root/.ssh/authorized_keys.tmp
mv /root/.ssh/authorized_keys.tmp /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
echo
echo "=========== COPY EVERYTHING BETWEEN THE LINES (including BEGIN/END) ==========="
cat "$KEY"
echo "================================================================================"
echo "Paste it into GitHub: repo → Settings → Secrets and variables → Actions → New repository secret"
echo "Name: VPS_SSH_KEY     Value: the block above.   Do not paste it anywhere else."
