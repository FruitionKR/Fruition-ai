"""TLS configuration fails closed; local clients retain their original defaults."""
import ast
import os
from pathlib import Path
import ssl
import unittest
from unittest.mock import patch
from app.core.kafka_security import kafka_security_options


class KafkaSecurityTests(unittest.TestCase):
    def test_every_kafka_client_uses_shared_transport_options(self):
        app = Path(__file__).resolve().parents[2] / "app"
        clients = []
        for source in app.rglob("*.py"):
            for node in ast.walk(ast.parse(source.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id not in {"AIOKafkaConsumer", "AIOKafkaProducer"}:
                    continue
                clients.append((source.name, node.lineno))
                self.assertTrue(any(k.arg is None and isinstance(k.value, ast.Call)
                                    and isinstance(k.value.func, ast.Name)
                                    and k.value.func.id == "kafka_security_options"
                                    for k in node.keywords), (source.name, node.lineno))
        self.assertTrue(clients)

    def test_local_default_does_not_change_client_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual({}, kafka_security_options())

    def test_unsupported_protocol_and_missing_credentials_fail_closed(self):
        for env in ({"KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT"},
                    {"KAFKA_SECURITY_PROTOCOL": "SSL"},
                    {"KAFKA_SECURITY_PROTOCOL": "SSL", "KAFKA_SSL_CAFILE": "ca.pem"}):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                kafka_security_options()

    def test_invalid_ca_never_falls_back_to_plaintext(self):
        env = {"KAFKA_SECURITY_PROTOCOL": "SSL", "KAFKA_SSL_CAFILE": "/nonexistent/kafka-ca.pem",
               "KAFKA_SSL_CERTFILE": "/nonexistent/user.crt", "KAFKA_SSL_KEYFILE": "/nonexistent/user.key"}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(FileNotFoundError):
            kafka_security_options()

    def test_mtls_keeps_certificate_and_hostname_verification(self):
        env = {"KAFKA_SECURITY_PROTOCOL": "SSL", "KAFKA_SSL_CAFILE": "ca.pem",
               "KAFKA_SSL_CERTFILE": "user.crt", "KAFKA_SSL_KEYFILE": "user.key"}
        # A real default context verifies both trust and hostname. Mock file loading only.
        context = ssl.create_default_context()
        with patch.dict(os.environ, env, clear=True), \
             patch("app.core.kafka_security.ssl.create_default_context", return_value=context) as create, \
             patch.object(ssl.SSLContext, "load_cert_chain") as load:
            opts = kafka_security_options()
        create.assert_called_once_with(cafile="ca.pem")
        load.assert_called_once_with("user.crt", "user.key")
        self.assertEqual("SSL", opts["security_protocol"])
        self.assertIs(context, opts["ssl_context"])
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertEqual(ssl.TLSVersion.TLSv1_2, context.minimum_version)


if __name__ == "__main__":
    unittest.main()
