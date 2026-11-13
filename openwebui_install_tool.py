"""Install or update the 📂 Door files tool in Open WebUI, the way its Workspace → Tools page does.
Run inside the open-webui container (golive-doors.sh does this):
  docker cp openwebui_door_files.py open-webui:/tmp/ && docker cp openwebui_install_tool.py open-webui:/tmp/
  docker exec -w /app/backend open-webui python3 /tmp/openwebui_install_tool.py /tmp/openwebui_door_files.py <owner user id>
  docker exec -w /app/backend open-webui python3 /tmp/openwebui_install_tool.py --remove
"""
import asyncio
import sys

sys.path.insert(0, "/app/backend")   # Open WebUI's own code, inside its container
from open_webui.models.tools import ToolForm, ToolMeta, Tools
from open_webui.utils.plugin import load_tool_module_by_id, replace_imports
from open_webui.utils.tools import get_tool_specs

TOOL_ID = "door_files"
NAME = "📂 Door files"


async def main(argv):
    if "--remove" in argv:
        print("removed" if await Tools.delete_tool_by_id(TOOL_ID) else "not there")
        return
    path, owner = argv[1], argv[2]
    content = replace_imports(open(path).read())
    module, frontmatter, _ = await load_tool_module_by_id(TOOL_ID, content=content)
    specs = get_tool_specs(module)
    meta = ToolMeta(description=frontmatter.get("description"), manifest=frontmatter,
                    has_user_valves=hasattr(module, "UserValves"))
    if await Tools.get_tool_by_id(TOOL_ID):
        await Tools.update_tool_by_id(TOOL_ID, {"name": NAME, "content": content, "specs": specs,
                                                "meta": meta.model_dump()}, module=module)
        print(f"updated {TOOL_ID}: {[s['name'] for s in specs]}")
    else:
        form = ToolForm(id=TOOL_ID, name=NAME, content=content, meta=meta)
        tool = await Tools.insert_new_tool(owner, form, specs, module=module)
        print(f"installed {TOOL_ID}: {[s['name'] for s in specs]}" if tool else "FAILED")


asyncio.run(main(sys.argv))
