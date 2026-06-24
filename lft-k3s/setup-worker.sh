#!/bin/bash
set -e

echo -e "\e[1;34m=================================================\e[0m"
echo -e "\e[1;34m      Iniciando Setup do Worker (Destroyer)      \e[0m"
echo -e "\e[1;34m=================================================\e[0m\n"

# 1. Dados do Master
echo -e "\e[1;33mInsira as informacoes exibidas pelo setup-master.sh:\e[0m"
read -p "MASTER_IP: " MASTER_IP
read -p "TOKEN    : " TOKEN
echo ""

# 2. Dependencias
echo -e "\e[1;33m[1/3] Instalando dependencias (NFS Client, curl)...\e[0m"
sudo apt-get update -qq
sudo apt-get install -y curl nfs-common

# 3. Tailscale
echo -e "\e[1;33m[2/3] Instalando Tailscale...\e[0m"
if ! command -v tailscale &>/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
echo -e "\e[1;32m-> Autentique o Tailscale se solicitado:\e[0m"
sudo tailscale up

WORKER_IP=$(tailscale ip -4)
echo -e "\e[1;32mIP do Worker (Tailscale): $WORKER_IP\e[0m\n"

# 4. K3s Agent
echo -e "\e[1;33m[3/3] Instalando K3s agent e conectando ao Master...\e[0m"
curl -sfL https://get.k3s.io | \
  K3S_URL="https://${MASTER_IP}:6443" \
  K3S_TOKEN="${TOKEN}" \
  sh -s - agent \
    --node-ip=${WORKER_IP} \
    --flannel-iface=tailscale0

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m  WORKER CONECTADO. Volte ao Master e informe:   \e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e "  WORKER_IP : \e[1;37m${WORKER_IP}\e[0m"
echo -e "\e[1;32m=================================================\e[0m\n"
