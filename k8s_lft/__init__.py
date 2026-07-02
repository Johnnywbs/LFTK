"""K3s-based translation of the Docker-orchestration parts of LFT.

This package is a PoC: it only covers the components exercised by the
2x2 factorial experiment (dash_client, dash_server, iperf). OVS/ONOS are
intentionally left out of this package -- see the migration plan doc for
their conceptual (DaemonSet/Deployment) mapping.
"""
