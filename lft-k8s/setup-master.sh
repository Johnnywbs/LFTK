#!/bin/bash
set -e

print_credentials() {
  TOKEN_FILE="/var/lib/rancher/k3s/server/node-token"
  if [ -f "$TOKEN_FILE" ]; then
    sudo chmod o+r "$TOKEN_FILE"
    echo -e "\n\e[1;33m=================================================\e[0m"
    echo -e "\e[1;33m USE ESTES DADOS NO DESTROYER:                   \e[0m"
    echo -e "\e[1;33m=================================================\e[0m"
    echo -e "MASTER_IP: \e[1;37m${MASTER_IP:-$(tailscale ip -4 2>/dev/null)}\e[0m"
    echo -e "TOKEN:     \e[1;37m$(cat $TOKEN_FILE)\e[0m"
    echo -e "\e[1;33m=================================================\e[0m\n"
  fi
}
trap print_credentials EXIT

echo -e "\e[1;34m=================================================\e[0m"
echo -e "\e[1;34m       Iniciando Setup do Master (Lenovo)        \e[0m"
echo -e "\e[1;34m=================================================\e[0m\n"

# 1. Dependências
echo -e "\e[1;33m[1/7] Instalando dependências (NFS, jq, curl)...\e[0m"
sudo apt-get update > /dev/null 2>&1
sudo apt-get install -y curl jq nfs-kernel-server wget > /dev/null 2>&1

# 2. Configurando Armazenamento NFS
echo -e "\e[1;33m[2/7] Configurando servidor de arquivos NFS...\e[0m"
sudo mkdir -p /srv/lft_results
sudo chmod 777 /srv/lft_results
echo "/srv/lft_results *(rw,sync,no_subtree_check,no_root_squash)" | sudo tee /etc/exports > /dev/null
sudo exportfs -a
sudo systemctl restart nfs-kernel-server
echo -e "\e[1;32m-> NFS exportando /srv/lft_results\e[0m"

# 3. Tailscale
echo -e "\e[1;33m[3/7] Instalando Tailscale...\e[0m"
curl -fsSL https://tailscale.com/install.sh | sh > /dev/null 2>&1
echo -e "\e[1;32m-> Por favor, autentique o Tailscale no link abaixo (se solicitado):\e[0m"
sudo tailscale up

MASTER_IP=$(tailscale ip -4)
echo -e "\e[1;32mIP do Master capturado: $MASTER_IP\e[0m\n"

# 4. Ajuste de MTU do Tailscale para suportar VXLAN sem fragmentação
# Tailscale MTU=1280, VXLAN overhead=50 bytes → MTU interno precisa ser ≤1230
echo -e "\e[1;33m[4/7] Ajustando MTU do Tailscale para VXLAN (1230)...\e[0m"
sudo mkdir -p /etc/systemd/network
cat <<EOF | sudo tee /etc/systemd/network/tailscale0.network > /dev/null
[Match]
Name=tailscale0

[Link]
MTUBytes=1230
EOF
# Aplica imediatamente sem reiniciar
sudo ip link set dev tailscale0 mtu 1230 2>/dev/null || true
echo -e "\e[1;32m-> MTU ajustado para 1230\e[0m"

# 5. Instalando K3s Server
echo -e "\e[1;33m[5/7] Instalando o K3s...\e[0m"
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server --node-ip=${MASTER_IP} --flannel-iface=tailscale0" sh -s - > /dev/null 2>&1

# Configurar kubectl para o usuário atual
mkdir -p $HOME/.kube
sudo cp /etc/rancher/k3s/k3s.yaml $HOME/.kube/config
sudo chown $USER:$USER $HOME/.kube/config
echo -e "\e[1;32m-> kubectl configurado para o usuário $USER\e[0m"

# Aguardar K3s ficar pronto
until kubectl get nodes > /dev/null 2>&1; do sleep 3; done
echo -e "\e[1;32m-> K3s pronto\e[0m"

# 6. Instalando Multus e Whereabouts
echo -e "\e[1;33m[6/7] Instalando Multus CNI e Whereabouts IPAM...\e[0m"
kubectl apply -f https://raw.githubusercontent.com/k8snetworkplumbingwg/multus-cni/master/deployments/multus-daemonset.yml > /dev/null 2>&1
kubectl apply -f https://raw.githubusercontent.com/k8snetworkplumbingwg/whereabouts/master/doc/crds/daemonset-install.yaml > /dev/null 2>&1
kubectl apply -f https://raw.githubusercontent.com/k8snetworkplumbingwg/whereabouts/master/doc/crds/whereabouts.cni.cncf.io_ippools.yaml > /dev/null 2>&1
kubectl apply -f https://raw.githubusercontent.com/k8snetworkplumbingwg/whereabouts/master/doc/crds/whereabouts.cni.cncf.io_overlappingrangeipreservations.yaml > /dev/null 2>&1
echo -e "\e[1;32m-> Multus e Whereabouts instalados\e[0m"

# 7. Criar symlink do plugin macvlan no bundle do K3s
echo -e "\e[1;33m[7/7] Habilitando plugin macvlan no K3s...\e[0m"
CNI_BIN=$(readlink -f /var/lib/rancher/k3s/data/cni/bridge)
if [ -z "$CNI_BIN" ]; then
  echo -e "\e[1;31m[ERRO] Binário CNI do K3s não encontrado.\e[0m"
  exit 1
fi
sudo ln -sf "$CNI_BIN" /var/lib/rancher/k3s/data/cni/macvlan
echo -e "\e[1;32m-> Symlink macvlan criado -> $CNI_BIN\e[0m"

# Verificar interface flannel.1
if ip link show flannel.1 > /dev/null 2>&1; then
  echo -e "\e[1;32m-> Interface flannel.1 encontrada — Multus usará macvlan sobre ela\e[0m"
else
  echo -e "\e[1;31m[ERRO] Interface flannel.1 nao encontrada.\e[0m"
  echo -e "\e[1;31m       Verifique se o K3s e o Flannel estao rodando: kubectl get pods -n kube-system\e[0m"
  exit 1
fi

# Abrir porta VXLAN no firewall
sudo ufw allow 4789/udp 2>/dev/null || true

# Labels no nó master
NODE_NAME=$(hostname); kubectl label node ${NODE_NAME,,} node-role=master --overwrite > /dev/null 2>&1

sudo chmod o+r /var/lib/rancher/k3s/server/node-token
TOKEN=$(cat /var/lib/rancher/k3s/server/node-token)

echo -e "\n\e[1;32m=================================================\e[0m"
echo -e "\e[1;32m SETUP CONCLUÍDO! USE ESTES DADOS NO DESTROYER:  \e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e "MASTER_IP: \e[1;37m$MASTER_IP\e[0m"
echo -e "TOKEN:     \e[1;37m$TOKEN\e[0m"
echo -e "\e[1;32m=================================================\e[0m"
echo -e "\n\e[1;33mPróximos passos:\e[0m"
echo -e "1. Execute setup-worker.sh no notebook Destroyer"
echo -e "2. No master, adicione o label no worker:"
echo -e "   \e[1;37mkubectl label node <nome-do-destroyer> lft-role=worker\e[0m"
echo -e "3. Execute o experimento: \e[1;37mbash lft-k8s/run.sh\e[0m\n"
