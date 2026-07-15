#!/bin/bash
set -e

echo -e "\e[1;34m=================================================\e[0m"
echo -e "\e[1;34m      Iniciando Setup do Worker (Destroyer)      \e[0m"
echo -e "\e[1;34m=================================================\e[0m\n"

# 1. Coletando Dados do Master
echo -e "\e[1;33mInsira as informações geradas pelo script do Master:\e[0m"
read -p "Digite o IP do Master (ex: 100.x.x.x): " MASTER_IP
read -p "Digite o TOKEN do Master: " TOKEN
echo ""

# 2. Dependências
echo -e "\e[1;33m[1/4] Instalando dependências (NFS Client)...\e[0m"
sudo apt-get update > /dev/null 2>&1
sudo apt-get install -y curl nfs-common > /dev/null 2>&1

# 3. Tailscale
echo -e "\e[1;33m[2/4] Instalando Tailscale...\e[0m"
curl -fsSL https://tailscale.com/install.sh | sh > /dev/null 2>&1
echo -e "\e[1;32m-> Por favor, autentique o Tailscale no link abaixo (se solicitado):\e[0m"
sudo tailscale up

WORKER_IP=$(tailscale ip -4)
echo -e "\e[1;32mIP do Worker capturado: $WORKER_IP\e[0m\n"

# 4. Instalando K3s Agent
echo -e "\e[1;33m[3/4] Instalando Agente do K3s e ancorando no Master...\e[0m"
curl -sfL https://get.k3s.io | K3S_URL="https://${MASTER_IP}:6443" K3S_TOKEN="${TOKEN}" sh -s - agent --node-ip=${WORKER_IP} --flannel-iface=tailscale0 > /dev/null 2>&1

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m WORKER CONFIGURADO E CONECTADO COM SUCESSO!     \e[0m"
echo -e "\e[1;32m=================================================\e[0m\n"
