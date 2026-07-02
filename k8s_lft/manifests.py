"""Builders that translate the Docker-CLI node definitions used by
onos_topologies/dash_topology/{dash_client,dash_server}.py and
onos_topologies/iperf_experiment/{iperf_client,iperf_server}.py into
typed Kubernetes objects from the official `kubernetes` client library.

Where the original code built a `docker run ...` shell string
(profissa_lft/node.py:Node.instantiate), the functions below build a
`kubernetes.client.V1*` object and return it -- nothing is applied to a
cluster here, see k8s_client.py for that.

Only dash_client, dash_server and iperf are covered (the components the
2x2 factorial experiment in experiment/factorial_experiment.py actually
exercises). OVS/ONOS are intentionally out of scope -- see the migration
plan for their conceptual DaemonSet/Deployment mapping.
"""
from __future__ import annotations

from kubernetes import client

DASH_SERVER_IMAGE = "neubot/dash:latest"
DASH_CLIENT_IMAGE = "neubot/dash-client:latest"
IPERF_IMAGE = "lft-iperf:latest"

LABEL_APP = "lft.io/app"
LABEL_ROLE = "lft.io/role"


# ---------------------------------------------------------------------------
# Shared helpers (kept private and in this module on purpose -- the plan
# explicitly avoids a separate common.py for a two-component PoC)
# ---------------------------------------------------------------------------

def _labels(name: str, role: str) -> dict:
    return {LABEL_APP: name, LABEL_ROLE: role}


def _node_selector(hostname: str | None) -> dict | None:
    if not hostname:
        return None
    return {"kubernetes.io/hostname": hostname}


def _pod_meta(name: str, role: str, namespace: str) -> client.V1ObjectMeta:
    return client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels(name, role))


# ---------------------------------------------------------------------------
# dash_server -- translation of onos_topologies/dash_topology/dash_server.py
# ---------------------------------------------------------------------------

def build_dash_server_deployment(
    name: str = "dash-server",
    namespace: str = "lft",
    image: str = DASH_SERVER_IMAGE,
    cert_secret_name: str = "dash-certs",
    node_selector_hostname: str | None = None,
) -> client.V1Deployment:
    """Mirrors dash_server.py's base_command exactly:

        -datadir /datadir -http-listen-address :80 -https-listen-address ''
        -prometheusx.listen-address :9999
        -tls-cert /certs/cert.pem -tls-key /certs/key.pem

    /datadir is an emptyDir (not a PVC): this PoC treats each run as
    ephemeral -- results are pulled out via k8s_client.copy_from_pod()
    right after the dash-client Job finishes, before the pod is deleted.
    /certs comes from a pre-existing Secret built from
    onos_topologies/dash_topology/certs/{cert.pem,key.pem}, e.g.:

        kubectl create secret generic dash-certs -n lft \\
            --from-file=onos_topologies/dash_topology/certs/cert.pem \\
            --from-file=onos_topologies/dash_topology/certs/key.pem
    """
    role = "dash-server"
    args = [
        "-datadir", "/datadir",
        "-http-listen-address", ":80",
        "-https-listen-address", "",
        "-prometheusx.listen-address", ":9999",
        "-tls-cert", "/certs/cert.pem",
        "-tls-key", "/certs/key.pem",
    ]
    container = client.V1Container(
        name=role,
        image=image,
        args=args,
        ports=[
            client.V1ContainerPort(container_port=80, name="http"),
            client.V1ContainerPort(container_port=9999, name="metrics"),
        ],
        volume_mounts=[
            client.V1VolumeMount(name="certs", mount_path="/certs", read_only=True),
            client.V1VolumeMount(name="datadir", mount_path="/datadir"),
        ],
    )
    pod_spec = client.V1PodSpec(
        containers=[container],
        node_selector=_node_selector(node_selector_hostname),
        volumes=[
            client.V1Volume(
                name="certs",
                secret=client.V1SecretVolumeSource(secret_name=cert_secret_name),
            ),
            client.V1Volume(name="datadir", empty_dir=client.V1EmptyDirVolumeSource()),
        ],
    )
    template = client.V1PodTemplateSpec(metadata=_pod_meta(name, role, namespace), spec=pod_spec)
    spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels=_labels(name, role)),
        template=template,
    )
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels(name, role)),
        spec=spec,
    )


def build_dash_server_service(
    name: str = "dash-server",
    namespace: str = "lft",
) -> client.V1Service:
    """ClusterIP Service, reachable cluster-wide (and cross-host, see plan
    section 3.1) as `<name>.<namespace>.svc.cluster.local` via CoreDNS."""
    spec = client.V1ServiceSpec(
        selector=_labels(name, "dash-server"),
        ports=[
            client.V1ServicePort(name="http", port=80, target_port=80),
            client.V1ServicePort(name="metrics", port=9999, target_port=9999),
        ],
        type="ClusterIP",
    )
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=spec,
    )


# ---------------------------------------------------------------------------
# dash_client -- translation of onos_topologies/dash_topology/dash_client.py
# and the `docker exec ... dash-client -y -hostname <ip> -scheme http`
# invocation in dash_experiment.py/dash_topology.py
# ---------------------------------------------------------------------------

def build_dash_client_job(
    name: str,
    server_url: str,
    namespace: str = "lft",
    image: str = DASH_CLIENT_IMAGE,
    scheme: str = "http",
    node_selector_hostname: str | None = None,
    ttl_seconds_after_finished: int = 300,
) -> client.V1Job:
    """Behavioral translation: the Docker version is a long-lived
    `sleep infinity` container later triggered via `docker exec`. Here the
    dash-client binary IS the container's main process -- the container
    runs to completion and the Job records success/failure, which is the
    natural K8s primitive for "one DASH run == one repetition"."""
    role = "dash-client"
    container = client.V1Container(
        name=role,
        image=image,
        command=["/usr/local/bin/dash-client"],
        args=["-y", "-hostname", server_url, "-scheme", scheme],
    )
    pod_spec = client.V1PodSpec(
        containers=[container],
        restart_policy="Never",
        node_selector=_node_selector(node_selector_hostname),
    )
    template = client.V1PodTemplateSpec(metadata=_pod_meta(name, role, namespace), spec=pod_spec)
    spec = client.V1JobSpec(
        template=template,
        backoff_limit=0,
        ttl_seconds_after_finished=ttl_seconds_after_finished,
    )
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels(name, role)),
        spec=spec,
    )


# ---------------------------------------------------------------------------
# iperf -- translation of onos_topologies/iperf_experiment/{iperf_server,iperf_client}.py
# ---------------------------------------------------------------------------

def build_iperf_server_deployment(
    name: str = "iperf-server",
    namespace: str = "lft",
    image: str = IPERF_IMAGE,
    port: int = 5201,
    node_selector_hostname: str | None = None,
) -> client.V1Deployment:
    """docker/iperf/Dockerfile sets ENTRYPOINT ["bash", "-lc"], so the
    original `docker exec -d <name> iperf3 -s -p <port>` relies on a shell
    already running inside the container. Here we override the
    command/args directly so the container's main process IS `iperf3 -s`,
    matching the Job/Deployment run-to-completion-or-forever model."""
    role = "iperf-server"
    container = client.V1Container(
        name=role,
        image=image,
        command=["iperf3"],
        args=["-s", "-p", str(port)],
        ports=[client.V1ContainerPort(container_port=port, name="iperf")],
    )
    pod_spec = client.V1PodSpec(
        containers=[container],
        node_selector=_node_selector(node_selector_hostname),
    )
    template = client.V1PodTemplateSpec(metadata=_pod_meta(name, role, namespace), spec=pod_spec)
    spec = client.V1DeploymentSpec(
        replicas=1,
        selector=client.V1LabelSelector(match_labels=_labels(name, role)),
        template=template,
    )
    return client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels(name, role)),
        spec=spec,
    )


def build_iperf_server_service(
    name: str = "iperf-server",
    namespace: str = "lft",
    port: int = 5201,
) -> client.V1Service:
    spec = client.V1ServiceSpec(
        selector=_labels(name, "iperf-server"),
        ports=[client.V1ServicePort(name="iperf", port=port, target_port=port)],
        type="ClusterIP",
    )
    return client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=spec,
    )


def build_iperf_client_job(
    name: str,
    server_host: str,
    namespace: str = "lft",
    image: str = IPERF_IMAGE,
    port: int = 5201,
    duration_s: int = 30,
    node_selector_hostname: str | None = None,
    ttl_seconds_after_finished: int = 300,
) -> client.V1Job:
    role = "iperf-client"
    container = client.V1Container(
        name=role,
        image=image,
        command=["iperf3"],
        args=["-c", server_host, "-p", str(port), "-t", str(duration_s), "-J"],
    )
    pod_spec = client.V1PodSpec(
        containers=[container],
        restart_policy="Never",
        node_selector=_node_selector(node_selector_hostname),
    )
    template = client.V1PodTemplateSpec(metadata=_pod_meta(name, role, namespace), spec=pod_spec)
    spec = client.V1JobSpec(
        template=template,
        backoff_limit=0,
        ttl_seconds_after_finished=ttl_seconds_after_finished,
    )
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=_labels(name, role)),
        spec=spec,
    )
