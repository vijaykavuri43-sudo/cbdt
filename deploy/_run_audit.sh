#!/bin/bash
set -euo pipefail
sudo sed -i 's/\r$//' /tmp/_audit_wallets.py
sudo docker cp /tmp/_audit_wallets.py s3lm:/tmp/audit_wallets.py
sudo docker exec s3lm python3 /tmp/audit_wallets.py
