"""Talk to the server over stdio like a real MCP client would.

Run:  uv run python tests/test_stdio_client.py
"""

import asyncio
import shutil
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "follower_rerouted.sch"


async def main():
    src = Path(r"C:\Program Files\Qucs-S\share\qucs-s\examples\ngspice\General Electronics\2N3904_follower.sch")
    shutil.copy(src, EXAMPLE)
    params = StdioServerParameters(command=sys.executable, args=["-m", "qucs_s_mcp.server"], cwd=str(ROOT))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", len(tools.tools))
            res = await s.call_tool("open_schematic", {"path": str(EXAMPLE)})
            print("open:", res.content[0].text[:300])
            res = await s.call_tool("tidy_schematic", {"reroute_all": True})
            print("tidy:", res.content[0].text[:800])
            res = await s.call_tool("move_components", {"moves": [{"name": "R1", "dx": 60}]})
            print("move:", res.content[0].text[:400])
            res = await s.call_tool("snapshot", {"nets": True, "issues": True, "title": "follower rerouted"})
            kinds = [c.type for c in res.content]
            print("snapshot content types:", kinds)
            img = next(c for c in res.content if c.type == "image")
            print("image mime:", img.mime_type, "base64 len:", len(img.data))
            print(res.content[0].text[:600])
            res = await s.call_tool("run_simulation", {})
            print("sim:", res.content[0].text[:300])


asyncio.run(main())
