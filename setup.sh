#!/bin/bash
# Puts the Paragon server on this machine and keeps it running.
# Safe to run again: it replaces the program, never the data.
set -e
export DEBIAN_FRONTEND=noninteractive
timedatectl set-timezone Asia/Karachi || true

mkdir -p /opt/paragon
cd /opt/paragon
B=https://raw.githubusercontent.com/waqarift-rgb/paragon-deploy/main
curl -fsSL -o paragon_server.py.new "$B/paragon_server.py"
curl -fsSL -o "Paragon Business App.html" "$B/Paragon%20Business%20App.html"

# Keep the secret from any earlier run; otherwise make a new one, so the
# default one in the file never faces the internet.
if [ -s /root/paragon-secret.txt ]; then
  S=$(cat /root/paragon-secret.txt)
else
  S=$(openssl rand -hex 12)
  echo "$S" > /root/paragon-secret.txt
  chmod 600 /root/paragon-secret.txt
fi
sed -i "s/^SHARED_SECRET = \"change-this-please\"/SHARED_SECRET = \"$S\"/" paragon_server.py.new
mv paragon_server.py.new paragon_server.py

cat > /etc/systemd/system/paragon.service <<'UNIT'
[Unit]
Description=Paragon server
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/paragon
ExecStart=/usr/bin/python3 /opt/paragon/paragon_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable paragon >/dev/null 2>&1
systemctl restart paragon

ufw allow 22/tcp  >/dev/null
ufw allow 80/tcp  >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 8080/tcp >/dev/null
ufw --force enable >/dev/null
sleep 3

clear
echo "================ PARAGON SERVER ================"
echo "files (first 16 of sha256):"
cd /opt/paragon
sha256sum "Paragon Business App.html" | cut -c1-16
echo "  (the server file is changed by its secret, so it is not compared)"
echo
echo -n "service:  "; systemctl is-active paragon
echo -n "firewall: "; ufw status | head -1
echo -n "answers:  "
curl -s -m 5 -X POST -H "Content-Type: text/plain" \
  -d "{\"key\":\"$S\",\"action\":\"ping\"}" http://127.0.0.1:8080/ | head -c 60; echo
echo -n "the app:  "; curl -s -m 5 -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/app
echo
echo "secret saved in /root/paragon-secret.txt  (read it with: cat /root/paragon-secret.txt)"
echo "================================================"
