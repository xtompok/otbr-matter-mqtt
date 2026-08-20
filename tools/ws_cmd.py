"""Send one command to python-matter-server over its websocket API."""
import asyncio
import json
import sys

import aiohttp


async def main() -> None:
    command = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 60
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect("ws://localhost:5580/ws") as ws:
            server_info = await ws.receive_json()
            print("server:", json.dumps(server_info)[:200], file=sys.stderr)
            await ws.send_json(
                {"message_id": "1", "command": command, "args": args}
            )
            while True:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
                if msg.get("message_id") == "1":
                    print(json.dumps(msg, indent=2))
                    if "error_code" in msg:
                        sys.exit(1)
                    return


asyncio.run(main())
