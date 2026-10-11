"""Laptop side of the AMD box tunnel: 127.0.0.1:18080-18083 -> box llama-server/embed/Laya/System 2.

The box's sshd refuses -L forwarding (TCP and Unix sockets), so each TCP connection accepted here runs
`python3 /root/stdio_bridge.py <port>` on the box over one multiplexed SSH connection (ControlMaster, opened by
tunnel.sh) and pipes bytes both ways. A new connection costs one SSH session (~0.8 s); HTTP keep-alive reuses it.
"""
import asyncio
import os

HOST = os.environ.get("SPOT_AMD_HOST", "spot-amd")
CTL = os.path.expanduser(os.environ.get("SPOT_AMD_CTL", "~/.ssh/spot-amd-ctl"))
ROUTES = {18080: 8080, 18081: 8081, 18082: 8082, 18083: 8083}   # llama-server, embed, Laya, System 2


async def pump(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def handler(remote_port):
    async def handle(lr, lw):
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-S", CTL, "-o", "BatchMode=yes", HOST, "python3", "/root/stdio_bridge.py", str(remote_port),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.gather(pump(lr, proc.stdin), pump(proc.stdout, lw))
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    return handle


async def main():
    servers = [await asyncio.start_server(handler(rp), "127.0.0.1", lp) for lp, rp in ROUTES.items()]
    print("listening", ", ".join(f"127.0.0.1:{lp} -> {HOST}:{rp}" for lp, rp in ROUTES.items()), flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
