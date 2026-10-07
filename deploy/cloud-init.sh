#!/bin/bash
# EC2 user-data: runs once on first boot (as root). Installs Docker Engine + the
# compose plugin and adds 2 GB swap so image builds fit on a 2 GB instance.
set -euxo pipefail

fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

curl -fsSL https://get.docker.com | sh
usermod -aG docker ubuntu
systemctl enable --now docker

touch /var/lib/cloud/sem7-ready
