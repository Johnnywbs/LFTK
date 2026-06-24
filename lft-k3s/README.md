# LFT-K3s

Experimento de validação para portabilidade do LFT (Lightweight Fog Testbed) para Kubernetes.

## Objetivo

Validar se o K3s consegue reproduzir o comportamento de rede controlada do LFT original
(Docker + OVS + tc netem) em dois experimentos progressivos.

| Experimento | Diretório | O que valida |
|---|---|---|
| 1 — netem | `manifests/` | tc netem cross-node via Tailscale + NFS + Multus intra-node |
| 2 — VXLAN | `manifests-vxlan/` | Multus cross-node real via tunnel VXLAN sobre Tailscale |

## Hardware

| Role | Hostname | IP Tailscale |
|---|---|---|
| Master | jonathas-santos-lenovo-ideapad-s145-15iwl | 100.69.222.7 |
| Worker | destroyer-vpceg17fb | (exibido pelo setup-worker.sh) |

## Setup

### 1. Master (Lenovo) — inicia o cluster e aguarda o Worker

```bash
chmod +x setup-master.sh
sudo ./setup-master.sh
```

O script instala K3s, Helm, Multus e Whereabouts, exibe `MASTER_IP` e `TOKEN`,
e depois aguarda você digitar o `WORKER_IP`.

### 2. Worker (Destroyer) — em outro terminal

```bash
chmod +x setup-worker.sh
sudo ./setup-worker.sh
```

Insira `MASTER_IP` e `TOKEN` quando solicitado. Ao final o script exibe o `WORKER_IP`.

### 3. Finalizar o Master

Cole o `WORKER_IP` no terminal do Master. Ele cria o ConfigMap VXLAN automaticamente.

### 4. Verificar cluster

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes
# Esperado: 2 nodes com status Ready
```

---

## Experimento 1 — tc netem

Valida: NFS funciona, tc netem é honrado pelo iperf, Multus cria interfaces intra-node.

```bash
kubectl apply -f manifests/1-storage.yaml
kubectl apply -f manifests/2-multus.yaml
kubectl apply -f manifests/3-netem.yaml
kubectl apply -f manifests/4-experimento.yaml
kubectl logs -f lft-iperf-client
```

Resultado salvo em `/srv/lft_results/iperf_report.json` no Master.

### Valores esperados

| Métrica | LFT (link degradado) | K3s esperado |
|---|---|---|
| Bandwidth | ~3 mbit/s | ~3 mbit/s |
| RTT | ~130 ms (one-way) | ~260 ms (netem aplicado nos dois nodes) |

> O netem é aplicado via DaemonSet em **ambos** os nodes (egress de cada um),
> então o RTT total é aproximadamente o dobro do delay configurado.

---

## Experimento 2 — Multus VXLAN cross-node

Valida: pods em nodes diferentes se comunicam exclusivamente via interface Multus (net1),
sem passar pelo Flannel. Tráfego: `10.20.0.11` (Destroyer) → `10.20.0.10` (Lenovo).

```bash
kubectl apply -f manifests-vxlan/1-vxlan-setup.yaml
kubectl rollout status daemonset/lft-vxlan-setup
kubectl apply -f manifests-vxlan/2-multus-vxlan.yaml
kubectl apply -f manifests-vxlan/3-pods.yaml
kubectl logs -f lft-iperf-client-vxlan
```

Resultado salvo em `/srv/lft_results/iperf_report_vxlan.json` no Master.

### Verificar que o tráfego usou a interface Multus

```bash
# Deve mostrar eth0 (Flannel) + net1 (Multus 10.20.0.x)
kubectl exec lft-iperf-server-vxlan -- ip addr show
```

---

## Limpeza

```bash
# Experimento 1
kubectl delete -f manifests/

# Experimento 2
kubectl delete -f manifests-vxlan/
```
