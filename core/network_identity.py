"""
Hive Mind -- Network identity resolution.

Resolves a Docker container's identity from its source IP address
via reverse DNS lookup. Used by the secrets endpoint to identify
which mind is requesting a secret.
"""

import asyncio
import logging
import socket

log = logging.getLogger("hive-mind.network_identity")


async def resolve_container_name(ip: str) -> str | None:
    """Resolve a source IP to a container hostname via reverse DNS.

    Uses socket.gethostbyaddr in a thread to avoid blocking the event loop.
    Strips any domain suffix (e.g. 'bilby.hivemind' -> 'bilby').

    Args:
        ip: The source IP address to resolve.

    Returns:
        Short hostname of the container, or None if lookup fails.
    """
    try:
        hostname, _aliases, _addrs = await asyncio.to_thread(
            socket.gethostbyaddr, ip
        )
        # Strip domain suffix -- Docker DNS may return FQDN
        short_name = hostname.split(".")[0]
        return short_name
    except (socket.herror, socket.gaierror, OSError) as exc:
        log.debug("Failed to resolve container name for IP %s: %s", ip, exc)
        return None
