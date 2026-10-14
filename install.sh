#!/bin/sh
set -eu
project=/home/bestvcboy/dgx-thermal
test "$(id -un)" = bestvcboy
test -f "$project/service.py"
umask 077
mkdir -p "$project/state"
chmod 700 "$project/state"
if [ ! -x "$project/.venv/bin/python" ]; then
    python3 -m venv "$project/.venv"
fi
"$project/.venv/bin/pip" install --disable-pip-version-check -r "$project/requirements.txt"
"$project/.venv/bin/python" -m unittest discover -s "$project" -p 'test_*.py'
if [ ! -f "$project/state/tls.crt" ]; then
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
        -keyout "$project/state/tls.key" -out "$project/state/tls.crt" \
        -subj '/CN=DGX thermal 192.168.3.180' \
        -addext 'subjectAltName=IP:192.168.3.180,IP:127.0.0.1,DNS:localhost' 2>/dev/null
fi
sudo install -m 644 "$project/dgx-thermal.service" /etc/systemd/system/dgx-thermal.service
sudo systemctl daemon-reload
sudo systemctl enable --now dgx-thermal.service
sudo systemctl restart dgx-thermal.service
systemctl is-active dgx-thermal.service
systemctl is-enabled dgx-thermal.service
printf 'UI https://192.168.3.180:8910/\nTrusted LAN settings save directly over HTTPS.\n'
