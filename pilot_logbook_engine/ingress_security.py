"""Small, testable allowlist for Home Assistant's Ingress proxy."""
INGRESS_PROXY_IP = "172.30.32.2"

def is_trusted_ingress_proxy(remote_addr):
    """Only Home Assistant Supervisor's Ingress proxy may reach the app."""
    return remote_addr == INGRESS_PROXY_IP
