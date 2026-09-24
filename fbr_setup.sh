#!/bin/bash
# ============================================================
#  FBR Invoice Manager Bridge — Vultr server setup
#  Yeh bridge ko server par install + hamesha chalu karta hai
#  (Paragon app se alag — port 5000)
# ============================================================
set -e

echo "================ FBR BRIDGE SETUP ================"

# 1. Folder
mkdir -p /opt/fbr
cd /opt/fbr

# 2. Bridge file download (GitHub se)
B=https://raw.githubusercontent.com/waqarift-rgb/paragon-deploy/main
curl -fsSL -o fbr_bridge.py.new "$B/fbr_bridge.py"

# validate
python3 -c "import ast; ast.parse(open('fbr_bridge.py.new').read())" && mv fbr_bridge.py.new fbr_bridge.py
curl -fsSL -o invoice_manager.html "$B/invoice_manager.html"

# 3. clients.json mehfooz rakho (agar pehle se hai)
if [ ! -f /opt/fbr/clients.json ]; then
  echo '{}' > /opt/fbr/clients.json
  chmod 600 /opt/fbr/clients.json
fi

# 4. Port 5000 firewall mein kholo
ufw allow 5000/tcp 2>/dev/null || iptables -I INPUT -p tcp --dport 5000 -j ACCEPT 2>/dev/null || true

# 5. systemd service (hamesha chale, reboot par bhi)
cat > /etc/systemd/system/fbr-bridge.service <<'EOF'
[Unit]
Description=FBR Invoice Bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/fbr/fbr_bridge.py
WorkingDirectory=/opt/fbr
Restart=always
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now fbr-bridge
sleep 2

# 6. Status
echo ""
echo "================ RESULT ================"
systemctl is-active fbr-bridge && echo "service:  active" || echo "service:  FAILED"
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR-SERVER-IP")
echo "bridge:   http://$SERVER_IP:5000/"
echo "admin:    http://$SERVER_IP:5000/admin"
echo "companies: $(python3 -c "import json; print(len(json.load(open('/opt/fbr/clients.json'))))" 2>/dev/null || echo 0)"
echo "========================================"
echo ""
echo "Ab browser mein admin kholein aur companies add karein:"
echo "  http://$SERVER_IP:5000/admin"
