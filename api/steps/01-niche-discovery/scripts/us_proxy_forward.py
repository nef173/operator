#!/usr/bin/env python3
"""Local no-auth proxy on 127.0.0.1:8888 that forwards to the authenticated US upstream
proxy, injecting Proxy-Authorization. Lets Chrome use --proxy-server=http://127.0.0.1:8888
so manual browsing + CDP both work through the US IP without Chrome handling proxy auth.
"""
import socket, threading, base64, select, sys

UP_HOST, UP_PORT = "89.42.86.163", 12323
UP_USER, UP_PASS = "14adedd3c347b", "250b12af0b"
AUTH = b"Proxy-Authorization: Basic " + base64.b64encode(f"{UP_USER}:{UP_PASS}".encode()) + b"\r\n"
LISTEN = ("127.0.0.1", 8888)


def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r: break
            for s in r:
                data = s.recv(65536)
                if not data: return
                (b if s is a else a).sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try: s.close()
            except Exception: pass


def handle(client):
    try:
        client.settimeout(30)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk: client.close(); return
            head += chunk
        header_part, _, rest = head.partition(b"\r\n\r\n")
        lines = header_part.split(b"\r\n")
        # strip any existing proxy-auth, inject ours
        lines = [l for l in lines if not l.lower().startswith(b"proxy-authorization")]
        new_head = lines[0] + b"\r\n" + AUTH + b"\r\n".join(lines[1:]) + b"\r\n\r\n"
        up = socket.create_connection((UP_HOST, UP_PORT), timeout=30)
        up.sendall(new_head + rest)
        pipe(client, up)
    except Exception:
        try: client.close()
        except Exception: pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN); srv.listen(128)
    print(f"forwarding proxy on {LISTEN} -> {UP_HOST}:{UP_PORT}", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    main()
