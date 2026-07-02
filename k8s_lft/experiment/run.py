"""Interactive entrypoint for the factorial experiment: prompts for
configuration instead of a long argparse command line, and pauses at
meaningful points to confirm before touching real infrastructure. Mirrors
the input()-based confirmation already used by
onos_topologies/dash_topology/dash_experiment.py ("Controller host
discovery? [y/N]").

Wraps factorial_experiment.main() and anova.main() -- no logic is
duplicated here, this is purely a friendlier front door for them.

Run as: python3 k8s_lft/experiment/run.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from k8s_lft.experiment import anova, factorial_experiment as fe  # noqa: E402


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or default


def _confirm(msg: str, default_yes: bool = False) -> bool:
    hint = "S/n" if default_yes else "s/N"
    val = input(f"{msg} [{hint}]: ").strip().lower()
    if not val:
        return default_yes
    return val in ("s", "sim", "y", "yes")


def gather_config() -> dict:
    print("=== Configuração do experimento LFT: Docker vs K3s ===\n")
    cfg = {
        "server_ip": _prompt("IP Tailscale do host SERVIDOR"),
        "client_ip": _prompt("IP Tailscale do host CLIENTE"),
        "server_hostname": _prompt("Nome do nó K8s do host SERVIDOR (ver `kubectl get nodes`)"),
        "client_hostname": _prompt("Nome do nó K8s do host CLIENTE"),
        "ssh_user": _prompt("Usuário SSH", default="ubuntu"),
        "ssh_key": _prompt("Caminho da chave SSH", default=str(Path.home() / ".ssh" / "id_ed25519")),
        "kube_config": _prompt("Caminho do kubeconfig", default=str(Path.home() / ".kube" / "config")),
        "namespace": _prompt("Namespace K8s", default="lft"),
        "remote_datadir": _prompt(
            "Diretório remoto do datadir no host servidor (condição Docker)",
            default="/home/ubuntu/lft_datadir",
        ),
        "remote_certs": _prompt(
            "Diretório remoto dos certs no host servidor (condição Docker)",
            default="/home/ubuntu/lft_certs",
        ),
        "out": _prompt("Diretório de saída dos resultados", default="results/factorial"),
        "seed": _prompt("Seed (reprodutibilidade da ordem das execuções)", default="20260702"),
    }
    # os campos abaixo não têm um default seguro -- insiste até serem preenchidos
    while not (cfg["server_ip"] and cfg["client_ip"] and cfg["server_hostname"] and cfg["client_hostname"]):
        print("\nIP Tailscale e nome do nó K8s dos dois hosts são obrigatórios.\n")
        cfg["server_ip"] = cfg["server_ip"] or _prompt("IP Tailscale do host SERVIDOR")
        cfg["client_ip"] = cfg["client_ip"] or _prompt("IP Tailscale do host CLIENTE")
        cfg["server_hostname"] = cfg["server_hostname"] or _prompt("Nome do nó K8s do host SERVIDOR")
        cfg["client_hostname"] = cfg["client_hostname"] or _prompt("Nome do nó K8s do host CLIENTE")

    cfg["ssh_key"] = os.path.expanduser(cfg["ssh_key"]) if cfg["ssh_key"] else ""
    cfg["kube_config"] = os.path.expanduser(cfg["kube_config"]) if cfg["kube_config"] else ""
    return cfg


def build_argv(cfg: dict, r: int, out_dir: str) -> list[str]:
    argv = [
        "--r", str(r), "--seed", cfg["seed"], "--out", out_dir,
        "--server-tailscale-ip", cfg["server_ip"], "--client-tailscale-ip", cfg["client_ip"],
        "--server-hostname", cfg["server_hostname"], "--client-hostname", cfg["client_hostname"],
        "--ssh-user", cfg["ssh_user"],
        "--namespace", cfg["namespace"],
        "--remote-datadir", cfg["remote_datadir"], "--remote-certs", cfg["remote_certs"],
    ]
    if cfg["ssh_key"]:
        argv += ["--ssh-key", cfg["ssh_key"]]
    if cfg["kube_config"]:
        argv += ["--kube-config", cfg["kube_config"]]
    return argv


def main() -> None:
    cfg = gather_config()

    smoke_out = str(Path(cfg["out"]) / "smoke")
    if _confirm("\nRodar um smoke test rápido (r=2, 8 execuções) antes do experimento completo?", default_yes=True):
        print(f"\n>>> Smoke test (r=2) em {smoke_out} ...\n")
        fe.main(build_argv(cfg, r=2, out_dir=smoke_out))
        print(f"\nSmoke test concluído. Confira {smoke_out}/results.csv antes de continuar.")
        if not _confirm("\nOs resultados do smoke test parecem corretos? Continuar para o experimento completo?"):
            print("Interrompido pelo usuário após o smoke test.")
            return

    r_str = _prompt("\nRepetições por célula no experimento completo (r)", default="30")
    r = int(r_str) if r_str.strip().isdigit() else 30

    print(
        f"\nIsso vai criar/destruir containers Docker e recursos Kubernetes nos hosts "
        f"{cfg['server_ip']} e {cfg['client_ip']} repetidamente, {4 * r} vezes ao todo."
    )
    if not _confirm("Confirma o início do experimento completo?"):
        print("Cancelado pelo usuário.")
        return

    print(f"\n>>> Experimento completo (r={r}) em {cfg['out']} ...\n")
    fe.main(build_argv(cfg, r=r, out_dir=cfg["out"]))
    csv_path = str(Path(cfg["out"]) / "results.csv")

    if _confirm(f"\nRodar a análise ANOVA agora sobre {csv_path}?", default_yes=True):
        anova_out = str(Path(cfg["out"]) / "anova")
        anova.main(["--csv", csv_path, "--out", anova_out])
        print(f"\nTabelas ANOVA salvas em {anova_out}")


if __name__ == "__main__":
    main()
