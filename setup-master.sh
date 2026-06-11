#!/bin/bash
set -e

echo -e "\e[1;34m=================================================\e[0m"
echo -e "\e[1;34m       Iniciando Setup do Master (Lenovo)        \e[0m"
echo -e "\e[1;34m=================================================\e[0m\n"

# 1. Dependências
echo -e "\e[1;33m[1/5] Instalando dependências (NFS, jq, curl)...\e[0m"
sudo apt-get update > /dev/null 2>&1
sudo apt-get install -y curl jq nfs-kernel-server > /dev/null 2>&1

# 2. Configurando Armazenamento NFS (Para o PVC)
echo -e "\e[1;33m[2/5] Configurando servidor de arquivos NFS...\e[0m"
sudo mkdir -p /srv/lft_results
sudo chmod 777 /srv/lft_results
echo "/srv/lft_results *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee /etc/exports > /dev/null
sudo exportfs -a
sudo systemctl restart nfs-kernel-server

# 3. Tailscale
echo -e "\e[1;33m[3/5] Instalando Tailscale...\e[0m"
curl -fsSL https://tailscale.com/install.sh | sh > /dev/null 2>&1
echo -e "\e[1;32m-> Por favor, autentique o Tailscale no link abaixo (se solicitado):\e[0m"
sudo tailscale up

MASTER_IP=$(tailscale ip -4)
echo -e "\e[1;32mIP do Master capturado: $MASTER_IP\e[0m\n"

# 4. Instalando K3s Server
echo -e "\e[1;33m[4/5] Instalando o K3s...\e[0m"
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --node-ip=${MASTER_IP} --flannel-iface=tailscale0" sh -s - > /dev/null 2>&1

# 5. Capturando credenciais
echo -e "\e[1;33m[5/5] Extraindo Token do Cluster...\e[0m"
TOKEN=$(sudo cat /var/lib/rancher/k3s/server/node-token)

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m SETUP CONCLUÍDO! USE ESTES DADOS NO DESTROYER:  \e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e "MASTER_IP: \e[1;37m$MASTER_IP\e[0m"
echo -e "TOKEN: \e[1;37m$TOKEN\e[0m"
echo -e "\e[1;32m=================================================\e[0m\n"
