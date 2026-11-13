# DRAFT upstream report: glab repeats `Private-Token` on a cross-origin redirect

Status: DRAFT, UNFILED. forgectl's maintainer files this on the `glab` project (#37). Do not file it from an automated session. When it is filed, put the link in the "Upstream report" section of `docs/design.md`. The text below is the issue body; it holds no private names, hosts or tokens.

## Title

`glab api` sends the `Private-Token` header to a different origin when the configured host answers with a redirect

## Summary

When the host `glab` is configured for answers a request with a 302 whose `Location` points at another origin, `glab` follows the redirect and sends the same `Private-Token` header to that origin. The reproduction below changes only the port in one run and only the hostname (`localhost` instead of `127.0.0.1`, the same listener) in another, and the token is sent in both. Go's standard client drops `Authorization` when a redirect goes to a different hostname, but a custom header such as `Private-Token` is kept. A server (or a misconfigured proxy, or a host that was renamed to someone else's address) that can issue a redirect can therefore collect the token.

## Environment

- glab 1.89.0 (c6fca530), Linux
- Token type: any; the reproduction uses a made-up string
- No network beyond 127.0.0.1

## Expected

The token header is sent only to the origin the user configured. On a redirect to another origin (scheme, hostname or port differs) `glab` drops `Private-Token` and `Job-Token` and similar credential headers, or stops and reports the redirect.

## Observed

Origin B receives `Private-Token: <the token>` for a GET and for a POST that the 302 turned into a GET. For comparison, an earlier local check (recorded in forgectl issue #37, with no script here and not re-tested for this report) found that `gh` 2.87.3 drops `Authorization` when redirected to another host, a subdomain or an explicit `:443`.

## Reproduction

Two loopback servers play two origins: A (configured) redirects to B. The script prints what each one received.

```python
import http.server, os, subprocess, tempfile, threading

FAKE = "FAKE-loopback-token-not-real"
seen = {"A": [], "B": []}


def server(name, redirect_to=None):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen[name].append((self.command, self.path, self.headers.get("Private-Token")))
            if redirect_to:
                self.send_response(302)
                self.send_header("Location", redirect_to + "/api/v4/projects/1")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = b'{"id": 1}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *a):
            pass

    return http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)


b = server("B")
threading.Thread(target=b.serve_forever, daemon=True).start()

# Run 1 changes only the port in the redirect; run 2 also changes the hostname (the listener
# is the same 127.0.0.1 one, "localhost" just names it differently).
for label, redirect_host in (("other port", "127.0.0.1"), ("other hostname", "localhost")):
    seen["A"].clear()
    seen["B"].clear()
    a = server("A", f"http://{redirect_host}:{b.server_address[1]}")
    threading.Thread(target=a.serve_forever, daemon=True).start()
    home = tempfile.mkdtemp()
    host = f"127.0.0.1:{a.server_address[1]}"
    cfg = os.path.join(home, "config.yml")
    with open(cfg, "w") as f:
        f.write(f'hosts:\n  "{host}":\n    api_protocol: http\n    api_host: {host}\n')
    os.chmod(cfg, 0o600)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": home,
        "GLAB_CONFIG_DIR": home,
        "GITLAB_HOST": host,
        "GITLAB_TOKEN": FAKE,
        "GLAB_NO_PROMPT": "1",
        "GLAB_SEND_TELEMETRY": "false",
        "GLAB_CHECK_UPDATE": "false",
        "GLAB_SHOW_WHATS_NEW": "false",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    for args in (["projects/1"], ["-X", "POST", "projects/1"]):
        subprocess.run(["glab", "api", *args], env=env, cwd=home, capture_output=True, timeout=30)
    print(label, "B:", seen["B"])
    a.shutdown()
```

Output with glab 1.89.0: it prints one line per run, `other port B: [...]` and `other hostname B: [...]`. In each, origin B received exactly two GET requests (`/api/v4/projects/1`), one for the GET and one for the POST that the 302 turned into a GET, both with `Private-Token: FAKE-loopback-token-not-real`.

## Suggested fix

Use an HTTP client `CheckRedirect` that removes credential headers (`Private-Token`, `Job-Token`, `Deploy-Token`, and `Authorization`) when the redirect's scheme, host or port differs from the original request's.

## Notes

- Redirects within the configured origin are unaffected.
- Only `glab api` was tested.
