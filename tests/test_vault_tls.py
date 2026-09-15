"""TLS must authenticate the server, including its hostname and trust anchor."""

import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from scripts.vault_tls import issue_identity


def test_vault_tls_validates_ca_and_hostname(tmp_path):
    identity = issue_identity()
    for name, value in identity.items():
        (tmp_path / name).write_text(value, encoding="ascii")
    (tmp_path / "wrong.crt").write_text(issue_identity()["ca.crt"], encoding="ascii")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(tmp_path / "server.crt", tmp_path / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://127.0.0.1:{server.server_port}"
    try:
        assert (
            requests.get(url, verify=str(tmp_path / "ca.crt"), timeout=3).status_code
            == 200
        )
        with pytest.raises(requests.exceptions.SSLError):
            requests.get(url, verify=str(tmp_path / "wrong.crt"), timeout=3)
        with pytest.raises(requests.exceptions.SSLError):
            requests.get(url, timeout=3)
        client = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
        import socket

        with socket.create_connection(server.server_address, timeout=3) as connection:
            with pytest.raises(ssl.SSLCertVerificationError):
                client.wrap_socket(connection, server_hostname="wrong-host")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
