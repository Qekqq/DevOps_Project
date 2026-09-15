ui = true
disable_mlock = true
storage "file" { path = "/vault/file" }
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_cert_file = "/vault/tls/server.crt"
  tls_key_file = "/vault/tls/server.key"
  tls_min_version = "tls12"
}
api_addr = "https://vault:8200"
