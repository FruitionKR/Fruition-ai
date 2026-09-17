"""Kafka transport configuration shared by producers and consumers.

Local development keeps PLAINTEXT. AWS explicitly selects SSL with a Strimzi
client certificate. Missing or invalid TLS files fail before connecting; never
fall back to plaintext or disable hostname verification.
"""
import os
import ssl
from typing import Any


def kafka_security_options() -> dict[str, Any]:
    protocol = os.environ.get("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT").upper()
    if protocol == "PLAINTEXT":
        return {}
    if protocol != "SSL":
        raise ValueError("KAFKA_SECURITY_PROTOCOL must be PLAINTEXT or SSL")
    paths = {}
    for name in ("KAFKA_SSL_CAFILE", "KAFKA_SSL_CERTFILE", "KAFKA_SSL_KEYFILE"):
        value = os.environ.get(name, "").strip()
        if not value:
            raise ValueError(f"{name} is required for Kafka SSL")
        paths[name] = value
    context = ssl.create_default_context(cafile=paths["KAFKA_SSL_CAFILE"])
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(paths["KAFKA_SSL_CERTFILE"], paths["KAFKA_SSL_KEYFILE"])
    return {"security_protocol": "SSL", "ssl_context": context}
