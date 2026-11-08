# Test-only TLS certificate

`localhost.crt` / `localhost.key` are a **self-signed test certificate** for the
local HTTP/2 test server (`tests/h2server.py`). They are not a secret: the key
is committed on purpose so `python -m pytest` and `bench/http2_bench.py` work
without extra dependencies.

The certificate is valid for `DNS:localhost` and `IP:127.0.0.1`. Clients in the
tests and benchmarks connect with `verify=False`; never reuse this key for
anything else.

Regenerate (10-year validity):

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout tests/certs/localhost.key -out tests/certs/localhost.crt \
  -days 3650 -subj "//CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```
