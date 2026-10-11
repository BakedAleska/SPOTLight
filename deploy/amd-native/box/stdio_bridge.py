"""Pipe stdin/stdout to one loopback SPOT service: `ssh spot-amd python3 /root/stdio_bridge.py <port>`.

The platform sshd refuses TCP and Unix-socket forwarding (AllowTcpForwarding no; stream-local channels fail too),
so ../laptop/exec_tunnel.py runs this once per TCP connection over a multiplexed SSH session. Only the four service
ports are allowed.
"""
import socket
import sys
import threading

ALLOWED = {8080, 8081, 8082, 8083}    # llama-server, embed, Laya, System 2
port = int(sys.argv[1])
if port not in ALLOWED:
    sys.exit("port not allowed")
s = socket.create_connection(("127.0.0.1", port))
inp, out = sys.stdin.buffer.raw, sys.stdout.buffer.raw


def up():
    while data := inp.read(65536):
        s.sendall(data)
    # No shutdown(SHUT_WR) on EOF: uvicorn treats a half-close as a client disconnect and drops the reply.
    # The server closes the connection itself (HTTP/1.0, or its keep-alive timeout), which ends the loop below.


threading.Thread(target=up, daemon=True).start()
while data := s.recv(65536):
    out.write(data)
