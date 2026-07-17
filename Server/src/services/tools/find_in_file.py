from bisect import bisect_right
import re
from typing import Annotated, Any

from fastmcp import Context
from mcp.types import ToolAnnotations

from services.registry import mcp_for_unity_tool
from services.hashline import Snapshot, decode_unity_text_response, normalize_text_uri, snapshots
from services.hashline.protocol import snapshot_key
from services.tools import get_unity_instance_from_context
from transport.unity_transport import send_with_unity_instance
from transport.legacy.unity_connection import async_send_command_with_retry


@mcp_for_unity_tool(
    unity_target="manage_script",
    description="Search any project text file with a regex and return line numbers, excerpts, and editable hashline anchors.",
    annotations=ToolAnnotations(
        title="Find in File",
        readOnlyHint=True,
    ),
)
async def find_in_file(
    ctx: Context,
    uri: Annotated[str, "The resource URI to search under Assets/ or file path form supported by read_resource"],
    pattern: Annotated[str, "The regex pattern to search for"],
    project_root: Annotated[str | None, "Optional project root path"] = None,
    max_results: Annotated[int, "Cap results to avoid huge payloads"] = 200,
    ignore_case: Annotated[bool | str | None,
                           "Case insensitive search"] = True,
) -> dict[str, Any]:
    # project_root is currently unused but kept for interface consistency
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(
        f"Processing find_in_file: {uri} (unity_instance={unity_instance or 'default'})")

    # 1. Read file content via Unity
    read_resp = await send_with_unity_instance(
        async_send_command_with_retry,
        unity_instance,
        "manage_script",
        {
            "action": "read_text",
            "file": normalize_text_uri(uri),
        },
    )

    if not isinstance(read_resp, dict) or not read_resp.get("success"):
        return read_resp if isinstance(read_resp, dict) else {"success": False, "message": str(read_resp)}

    try:
        contents, data = decode_unity_text_response(read_resp)
        canonical_path = str(data["path"])
        snapshot = Snapshot.create(
            snapshot_key(unity_instance, canonical_path),
            str(data["sha256"]),
            str(data.get("encoding", "utf-8")),
            str(data.get("newline", "lf")),
            contents,
        )
        snapshots.put(snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        return {"success": False, "code": "E_WRITE", "message": f"Could not read text metadata: {exc}"}

    # 2. Perform regex search
    flags = re.MULTILINE
    # Handle ignore_case which can be boolean or string from some clients
    ic = ignore_case
    if isinstance(ic, str):
        ic = ic.lower() in ("true", "1", "yes")
    if ic:
        flags |= re.IGNORECASE

    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return {"success": False, "message": f"Invalid regex pattern: {e}"}

    # If the regex is not multiline specific (doesn't contain \n literal match logic),
    # we could iterate lines. But users might use multiline regexes.
    # Let's search the whole content and map back to lines.

    found = list(regex.finditer(contents))
    line_breaks = list(re.finditer(r"\r\n|\r|\n", contents))
    line_starts = [0] + [match.end() for match in line_breaks]

    results = []
    count = 0

    for m in found:
        if count >= max_results:
            break

        start_idx = m.start()
        end_idx = m.end()

        line_index = bisect_right(line_starts, start_idx) - 1
        line_num = line_index + 1
        if line_num > len(snapshot.anchors):
            continue

        line_start = line_starts[line_index]
        line_end = line_breaks[line_index].start() if line_index < len(line_breaks) else len(contents)

        line_content = contents[line_start:line_end]

        # Create excerpt
        # We can just return the line content as excerpt

        results.append({
            "hash": snapshot.anchors[line_num - 1],
            "line": line_num,
            "content": line_content.strip(),  # detailed match info?
            "match": m.group(0),
            "start": start_idx,
            "end": end_idx
        })
        count += 1

    return {
        "success": True,
        "data": {
            "matches": results,
            "count": len(results),
            "total_matches": len(found),
            "path": canonical_path,
            "file_sha256": snapshot.file_sha256,
        }
    }
