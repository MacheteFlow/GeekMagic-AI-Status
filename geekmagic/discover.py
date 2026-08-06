"""Finds the SmallTV on the local network by itself.

The point is not to ask the user for an IP address almost nobody knows by
heart: we scan our own subnet and recognise the device by its /v.json reply,
which no other appliance serves.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROBE_TIMEOUT = 1.2


def local_ipv4() -> list[str]:
    """IPv4 addresses of this machine, excluding loopback and link-local."""
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except socket.gaierror:
        pass
    # The UDP-to-an-external-address trick reveals the interface actually used
    # to reach the outside, which is usually the right one on multi-homed hosts.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        found.add(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    return [
        ip for ip in found
        if not ip.startswith("127.") and not ip.startswith("169.254.")
    ]


def candidate_networks() -> list[ipaddress.IPv4Network]:
    """The /24 networks this machine belongs to, deduplicated."""
    nets = []
    for ip in local_ipv4():
        try:
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
        except ValueError:
            continue
        if net not in nets:
            nets.append(net)
    return nets


def probe(host: str, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """Return device info if there is a SmallTV at that address."""
    try:
        with urllib.request.urlopen(f"http://{host}/v.json", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    model = str(data.get("m", ""))
    if "smalltv" in model.lower() or "geekmagic" in model.lower():
        return {"host": host, "model": model, "version": data.get("v", "")}
    return None


def scan(networks=None, workers: int = 64, timeout: float = PROBE_TIMEOUT) -> list[dict]:
    """Look for the device across all local subnets. Takes a few seconds."""
    nets = networks if networks is not None else candidate_networks()
    hosts = [str(h) for net in nets for h in net.hosts()]
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for found in pool.map(lambda h: probe(h, timeout), hosts):
            if found:
                results.append(found)
    return results


if __name__ == "__main__":
    print("Networks to scan:", ", ".join(str(n) for n in candidate_networks()))
    for dev in scan():
        print(f"  found {dev['model']} {dev['version']} at {dev['host']}")
