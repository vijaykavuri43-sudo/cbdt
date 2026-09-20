#!/bin/bash
set -euo pipefail
sudo docker exec s3lm sh -c 'echo S3LM_DATA=$S3LM_DATA; ls -la "$S3LM_DATA" 2>/dev/null | head; ls -la /root/.local/share/s3-live-monitor 2>/dev/null | head; ls /data 2>/dev/null | head'
