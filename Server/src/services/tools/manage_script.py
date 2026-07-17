import base64
import os
from typing import Annotated, Any, Literal
from urllib.parse import urlparse, unquote

from fastmcp import Context
from mcp.types import ToolAnnotations

from services.registry import mcp_for_unity_tool
from services.hashline import Snapshot, apply_changes, build_read_response, decode_unity_text_response, normalize_text_uri, snapshots
from services.hashline.protocol import error, snapshot_key
from services.tools import get_unity_instance_from_context
from services.tools.refresh_unity import send_mutation
from transport.unity_transport import send_with_unity_instance
import transport.legacy.unity_connection

def _split_uri(uri: str) -> tuple[str, str]:
    """Split an incoming URI or path into (name, directory) suitable for Unity.

    Rules:
    - mcpforunity://path/Assets/... → keep as Assets-relative (after decode/normalize)
    - file://... → percent-decode, normalize, strip host and leading slashes,
        then, if any 'Assets' segment exists, return path relative to that 'Assets' root.
        Otherwise, fall back to original name/dir behavior.
    - plain paths → decode/normalize separators; if they contain an 'Assets' segment,
        return relative to 'Assets'.
    """
    raw_path: str
    if uri.startswith("mcpforunity://path/"):
        raw_path = uri[len("mcpforunity://path/"):]
    elif uri.startswith("file://"):
        parsed = urlparse(uri)
        host = (parsed.netloc or "").strip()
        p = parsed.path or ""
        # UNC: file://server/share/... -> //server/share/...
        if host and host.lower() != "localhost":
            p = f"//{host}{p}"
        # Use percent-decoded path, preserving leading slashes
        raw_path = unquote(p)
    else:
        raw_path = uri

    # Percent-decode any residual encodings and normalize separators
    raw_path = unquote(raw_path).replace("\\", "/")
    # Strip leading slash only for Windows drive-letter forms like "/C:/..."
    if os.name == "nt" and len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]

    # Normalize path (collapse ../, ./)
    norm = os.path.normpath(raw_path).replace("\\", "/")

    # If an 'Assets' segment exists, compute path relative to it (case-insensitive)
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    idx = next((i for i, seg in enumerate(parts)
                if seg.lower() == "assets"), None)
    assets_rel = "/".join(parts[idx:]) if idx is not None else None

    effective_path = assets_rel if assets_rel else norm
    # For POSIX absolute paths outside Assets, drop the leading '/'
    # to return a clean relative-like directory (e.g., '/tmp' -> 'tmp').
    if effective_path.startswith("/"):
        effective_path = effective_path[1:]

    name = os.path.splitext(os.path.basename(effective_path))[0]
    directory = os.path.dirname(effective_path)
    return name, directory


async def _read_hashline_snapshot(
    unity_instance: str | None,
    uri: str,
) -> tuple[Snapshot, dict[str, Any]] | dict[str, Any]:
    try:
        file_path = normalize_text_uri(uri)
    except ValueError as exc:
        return error("E_PATH", str(exc))
    response = await send_with_unity_instance(
        transport.legacy.unity_connection.async_send_command_with_retry,
        unity_instance,
        "manage_script",
        {"action": "read_text", "file": file_path},
        retry_on_reload=True,
    )
    if not isinstance(response, dict) or not response.get("success"):
        return response if isinstance(response, dict) else error("E_WRITE", str(response))
    try:
        contents, data = decode_unity_text_response(response)
        canonical_path = str(data["path"])
        sha256 = str(data["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        return error("E_WRITE", f"Invalid Unity text response: {exc}")
    key = snapshot_key(unity_instance, canonical_path)
    snapshot = Snapshot.create(
        key,
        sha256,
        str(data.get("encoding", "utf-8")),
        str(data.get("newline", "lf")),
        contents,
    )
    return snapshot, data


async def _read_text_hashlines(
    unity_instance: str | None,
    uri: str,
    offset: int = 1,
    limit: int = 200,
    raw: bool = False,
) -> dict[str, Any]:
    loaded = await _read_hashline_snapshot(unity_instance, uri)
    if isinstance(loaded, dict):
        return loaded
    snapshot, data = loaded
    if not raw:
        snapshots.put(snapshot)
    return build_read_response(
        snapshot,
        path=str(data["path"]),
        offset=offset,
        limit=limit,
        raw=raw,
    )


@mcp_for_unity_tool(
    unity_target="manage_script",
    description=(
        "Apply an atomic bulk hashline patch to any text file in the Unity project. "
        "Read the file first and submit changes with replace hash_range_inclusive, "
        "or prepend/append with an optional anchor. Coordinate and oldText/newText shapes "
        "are rejected with E_LEGACY_SHAPE."
    ),
    annotations=ToolAnnotations(title="Apply Hashline Text Edits", destructiveHint=True),
)
async def apply_text_edits(
    ctx: Context,
    uri: Annotated[str, "Project text file path or mcpforunity/file URI"],
    changes: Annotated[list[dict[str, Any]] | None, "Atomic hashline changes"] = None,
    options: Annotated[dict[str, Any] | None, "refresh and auto_read options"] = None,
    edits: Annotated[list[dict[str, Any]] | None, "Legacy coordinate edits; rejected"] = None,
    precondition_sha256: Annotated[str | None, "Legacy field; rejected"] = None,
    strict: Annotated[bool | None, "Legacy field; rejected"] = None,
    oldText: Annotated[str | None, "Legacy field; rejected"] = None,
    newText: Annotated[str | None, "Legacy field; rejected"] = None,
) -> dict[str, Any]:
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(f"Processing hashline apply_text_edits: {uri} (unity_instance={unity_instance or 'default'})")
    if edits is not None or precondition_sha256 is not None or strict is not None or oldText is not None or newText is not None:
        return error(
            "E_LEGACY_SHAPE",
            "Coordinate and oldText/newText edits are not executed. Read hashline anchors and resend with changes.",
            example={
                "changes": [{"hash_range_inclusive": ["ABC123", "ABC123"], "content_lines": ["replacement"]}]
            },
        )
    if isinstance(changes, list) and any(
        isinstance(change, dict)
        and any(field in change for field in ("startLine", "startCol", "endLine", "endCol", "oldText", "newText", "range", "operation"))
        for change in changes
    ):
        return error(
            "E_LEGACY_SHAPE",
            "Coordinate, range, operation, and oldText/newText edit shapes are not executed. Read hashline anchors and resend with changes.",
        )
    if options is not None and not isinstance(options, dict):
        return error("E_BAD_SHAPE", "options must be an object")
    unknown_options = sorted(set(options or {}) - {"refresh", "auto_read", "auto_read_context"})
    if unknown_options:
        return error("E_BAD_SHAPE", "options has unknown fields", unknown_fields=unknown_options)
    if "auto_read" in (options or {}) and not isinstance((options or {})["auto_read"], bool):
        return error("E_BAD_SHAPE", "auto_read must be a boolean")
    try:
        auto_context = max(0, min(20, int((options or {}).get("auto_read_context", 3))))
    except (TypeError, ValueError):
        return error("E_BAD_SHAPE", "auto_read_context must be an integer")

    resolved = await _read_hashline_snapshot(unity_instance, uri)
    if isinstance(resolved, dict):
        return resolved
    async with snapshots.lock_for(resolved[0].canonical_key):
        loaded = await _read_hashline_snapshot(unity_instance, uri)
        if isinstance(loaded, dict):
            return loaded
        current, read_data = loaded
        previous = snapshots.get(current.canonical_key)
        if previous is None:
            return error("E_BAD_REF", "No hashline snapshot exists for this file; read it before editing", reread_required=True)
        if previous.file_sha256 != current.file_sha256:
            return error(
                "E_STALE_ANCHOR",
                "The file changed after the anchors were read; no changes were applied",
                expected_sha256=previous.file_sha256,
                current_sha256=current.file_sha256,
                reread_required=True,
            )
        if current.newline == "mixed":
            return error("E_INVALID_PATCH", "Mixed newline files are readable but must be normalized explicitly before hashline editing")

        applied = apply_changes(previous, changes)
        if isinstance(applied, dict):
            return applied

        encoded = base64.b64encode(applied.text.encode("utf-8")).decode("ascii")
        params: dict[str, Any] = {
            "action": "apply_hashline_edits",
            "file": str(read_data["path"]),
            "encodedContents": encoded,
            "contentsEncoded": True,
            "precondition_sha256": current.file_sha256,
            "options": dict(options or {}),
        }

        async def _verify_after_disconnect():
            verified = await _read_hashline_snapshot(unity_instance, str(read_data["path"]))
            expected = Snapshot.create("verify", "", current.encoding, current.newline, applied.text)
            if (
                isinstance(verified, tuple)
                and verified[0].lines == expected.lines
                and verified[0].trailing_newline == expected.trailing_newline
            ):
                return {"success": True, "message": "Hashline edit applied and verified after reload.", "data": verified[1]}
            return None

        response = await send_mutation(
            ctx,
            unity_instance,
            "manage_script",
            params,
            verify_after_disconnect=_verify_after_disconnect,
        )
        if not isinstance(response, dict):
            return error("E_WRITE", str(response))
        if not response.get("success"):
            return response
        response_data = response.get("data") if isinstance(response.get("data"), dict) else {}
        new_sha = str(response_data.get("sha256") or "")
        if not new_sha:
            reread = await _read_hashline_snapshot(unity_instance, str(read_data["path"]))
            if isinstance(reread, dict):
                return reread
            _, reread_data = reread
            new_sha = str(reread_data["sha256"])

        updated = Snapshot.create(
            current.canonical_key,
            new_sha,
            current.encoding,
            current.newline,
            applied.text,
        )
        snapshots.put(updated)
        result: dict[str, Any] = {
            "success": True,
            "path": str(read_data["path"]),
            "changes_applied": applied.changes_applied,
            "lines_added": applied.lines_added,
            "lines_removed": applied.lines_removed,
            "file_sha256": new_sha,
            "diff_summary": f"+{applied.lines_added} -{applied.lines_removed}",
        }
        if (options or {}).get("auto_read", True):
            auto_offset = max(1, applied.changed_start + 1 - auto_context)
            auto_limit = min(120, max(1, applied.changed_end - applied.changed_start + (auto_context * 2)))
            auto_read = build_read_response(
                updated,
                path=str(read_data["path"]),
                offset=auto_offset,
                limit=auto_limit,
            )
            result["auto_read"] = auto_read.get("data", auto_read)
        else:
            result["auto_read"] = {"enabled": False}
        return result


@mcp_for_unity_tool(
    unity_target="manage_script",
    description="Create a new C# script at the given project path.",
    annotations=ToolAnnotations(
        title="Create Script",
        destructiveHint=True,
    ),
)
async def create_script(
    ctx: Context,
    path: Annotated[str, "Path under Assets/ to create the script at, e.g., 'Assets/Scripts/My.cs'"],
    contents: Annotated[str, "Contents of the script to create (plain text C# code). The server handles Base64 encoding."],
    script_type: Annotated[str, "Script type (e.g., 'C#')"] | None = None,
    namespace: Annotated[str, "Namespace for the script"] | None = None,
) -> dict[str, Any]:
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(
        f"Processing create_script: {path} (unity_instance={unity_instance or 'default'})")
    name = os.path.splitext(os.path.basename(path))[0]
    directory = os.path.dirname(path)
    # Local validation to avoid round-trips on obviously bad input
    norm_path = os.path.normpath(
        (path or "").replace("\\", "/")).replace("\\", "/")
    if not directory or directory.split("/")[0].lower() != "assets":
        return {"success": False, "code": "path_outside_assets", "message": f"path must be under 'Assets/'; got '{path}'."}
    if ".." in norm_path.split("/") or norm_path.startswith("/"):
        return {"success": False, "code": "bad_path", "message": "path must not contain traversal or be absolute."}
    if not name:
        return {"success": False, "code": "bad_path", "message": "path must include a script file name."}
    if not norm_path.lower().endswith(".cs"):
        return {"success": False, "code": "bad_extension", "message": "script file must end with .cs."}
    params: dict[str, Any] = {
        "action": "create",
        "name": name,
        "path": directory,
        "namespace": namespace,
        "scriptType": script_type,
    }
    if contents:
        params["encodedContents"] = base64.b64encode(
            contents.encode("utf-8")).decode("utf-8")
        params["contentsEncoded"] = True
    params = {k: v for k, v in params.items() if v is not None}

    async def _verify_create():
        verify = await send_with_unity_instance(
            transport.legacy.unity_connection.async_send_command_with_retry,
            unity_instance, "manage_script",
            {"action": "read", "name": name, "path": directory},
        )
        if isinstance(verify, dict) and verify.get("success"):
            return {"success": True, "message": "Script created (verified after domain reload).", "data": verify.get("data")}
        return None

    resp = await send_mutation(ctx, unity_instance, "manage_script", params, verify_after_disconnect=_verify_create)
    return resp if isinstance(resp, dict) else {"success": False, "message": str(resp)}


@mcp_for_unity_tool(
    unity_target="manage_script",
    description="Delete a C# script by URI or Assets-relative path.",
    annotations=ToolAnnotations(
        title="Delete Script",
        destructiveHint=True,
    ),
)
async def delete_script(
    ctx: Context,
    uri: Annotated[str, "URI of the script to delete under Assets/ directory, mcpforunity://path/Assets/... or file://... or Assets/..."],
) -> dict[str, Any]:
    """Delete a C# script by URI."""
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(
        f"Processing delete_script: {uri} (unity_instance={unity_instance or 'default'})")
    name, directory = _split_uri(uri)
    if not directory or directory.split("/")[0].lower() != "assets":
        return {"success": False, "code": "path_outside_assets", "message": "URI must resolve under 'Assets/'."}
    params = {"action": "delete", "name": name, "path": directory}

    async def _verify_delete():
        verify = await send_with_unity_instance(
            transport.legacy.unity_connection.async_send_command_with_retry,
            unity_instance, "manage_script",
            {"action": "read", "name": name, "path": directory},
        )
        if isinstance(verify, dict) and not verify.get("success"):
            return {"success": True, "message": "Script deleted (verified after domain reload)."}
        return None

    resp = await send_mutation(ctx, unity_instance, "manage_script", params, verify_after_disconnect=_verify_delete)
    return resp if isinstance(resp, dict) else {"success": False, "message": str(resp)}


@mcp_for_unity_tool(
    unity_target="manage_script",
    description="Validate a C# script and return diagnostics.",
    annotations=ToolAnnotations(
        title="Validate Script",
        readOnlyHint=True,
    ),
)
async def validate_script(
    ctx: Context,
    uri: Annotated[str, "URI of the script to validate under Assets/ directory, mcpforunity://path/Assets/... or file://... or Assets/..."],
    level: Annotated[Literal['basic', 'standard'],
                     "Validation level"] = "basic",
    include_diagnostics: Annotated[bool,
                                   "Include full diagnostics and summary"] = False,
) -> dict[str, Any]:
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(
        f"Processing validate_script: {uri} (unity_instance={unity_instance or 'default'})")
    name, directory = _split_uri(uri)
    if not directory or directory.split("/")[0].lower() != "assets":
        return {"success": False, "code": "path_outside_assets", "message": "URI must resolve under 'Assets/'."}
    if level not in ("basic", "standard"):
        return {"success": False, "code": "bad_level", "message": "level must be 'basic' or 'standard'."}
    params = {
        "action": "validate",
        "name": name,
        "path": directory,
        "level": level,
    }
    resp = await send_with_unity_instance(
        transport.legacy.unity_connection.async_send_command_with_retry,
        unity_instance,
        "manage_script",
        params,
    )
    if isinstance(resp, dict) and resp.get("success"):
        diags = resp.get("data", {}).get("diagnostics", []) or []
        warnings = sum(1 for d in diags if str(
            d.get("severity", "")).lower() == "warning")
        errors = sum(1 for d in diags if str(
            d.get("severity", "")).lower() in ("error", "fatal"))
        if include_diagnostics:
            return {"success": True, "data": {"diagnostics": diags, "summary": {"warnings": warnings, "errors": errors}}}
        return {"success": True, "data": {"warnings": warnings, "errors": errors}}
    return resp if isinstance(resp, dict) else {"success": False, "message": str(resp)}


@mcp_for_unity_tool(
    unity_target="manage_script",
    description="Read part of any text file in the Unity project. Hashline anchors are returned by default.",
    annotations=ToolAnnotations(title="Read Text Hashlines", readOnlyHint=True),
)
async def read_text(
    ctx: Context,
    uri: Annotated[str, "Project text file path or mcpforunity/file URI"],
    offset: Annotated[int, "1-indexed first line"] = 1,
    limit: Annotated[int, "Maximum lines to return (capped at 500)"] = 200,
    raw: Annotated[bool, "Return untagged text and do not update the editable snapshot"] = False,
) -> dict[str, Any]:
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(f"Processing read_text: {uri} (unity_instance={unity_instance or 'default'})")
    return await _read_text_hashlines(unity_instance, uri, offset, limit, raw)


@mcp_for_unity_tool(
    description="Compatibility router for script create/read/delete. Read returns partial hashline output; use read_text for non-C# files.",
    annotations=ToolAnnotations(
        title="Manage Script",
        destructiveHint=True,
    ),
)
async def manage_script(
    ctx: Context,
    action: Annotated[Literal['create', 'read', 'delete'], "Perform CRUD operations on C# scripts."],
    name: Annotated[str, "Script name (no .cs extension)", "Name of the script to create"],
    path: Annotated[str, "Asset path (default: 'Assets/')", "Path under Assets/ to create the script at, e.g., 'Assets/Scripts/My.cs'"],
    contents: Annotated[str, "Contents of the script to create",
                        "C# code for 'create' action"] | None = None,
    script_type: Annotated[str, "Script type (e.g., 'C#')",
                           "Type hint (e.g., 'MonoBehaviour')"] | None = None,
    namespace: Annotated[str, "Namespace for the script"] | None = None,
    offset: Annotated[int, "1-indexed first line for read"] = 1,
    limit: Annotated[int, "Maximum lines for read"] = 200,
    raw: Annotated[bool, "Return untagged read output"] = False,
) -> dict[str, Any]:
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(
        f"Processing manage_script: {action} (unity_instance={unity_instance or 'default'})")
    try:
        if action == "read":
            candidate = path.replace("\\", "/")
            if not os.path.splitext(os.path.basename(candidate))[1]:
                filename = name if os.path.splitext(name)[1] else f"{name}.cs"
                candidate = os.path.join(candidate, filename).replace("\\", "/")
            return await _read_text_hashlines(unity_instance, candidate, offset, limit, raw)

        # Prepare parameters for Unity
        params = {
            "action": action,
            "name": name,
            "path": path,
            "namespace": namespace,
            "scriptType": script_type,
        }

        # Base64 encode the contents if they exist to avoid JSON escaping issues
        if contents:
            if action == 'create':
                params["encodedContents"] = base64.b64encode(
                    contents.encode('utf-8')).decode('utf-8')
                params["contentsEncoded"] = True
            else:
                params["contents"] = contents

        params = {k: v for k, v in params.items() if v is not None}

        if action != "read":
            async def _verify_mutation():
                verify = await send_with_unity_instance(
                    transport.legacy.unity_connection.async_send_command_with_retry,
                    unity_instance, "manage_script",
                    {"action": "read", "name": name, "path": path},
                )
                if action == "create" and isinstance(verify, dict) and verify.get("success"):
                    return {"success": True, "message": "Script created (verified after domain reload).", "data": verify.get("data")}
                elif action == "delete" and isinstance(verify, dict) and not verify.get("success"):
                    return {"success": True, "message": "Script deleted (verified after domain reload)."}
                return None

            response = await send_mutation(ctx, unity_instance, "manage_script", params, verify_after_disconnect=_verify_mutation)

        if isinstance(response, dict):
            if response.get("success"):
                if response.get("data", {}).get("contentsEncoded"):
                    decoded_contents = base64.b64decode(
                        response["data"]["encodedContents"]).decode('utf-8')
                    response["data"]["contents"] = decoded_contents
                    del response["data"]["encodedContents"]
                    del response["data"]["contentsEncoded"]

                return {
                    "success": True,
                    "message": response.get("message", "Operation successful."),
                    "data": response.get("data"),
                }
            return response

        return {"success": False, "message": str(response)}

    except Exception as e:
        return {
            "success": False,
            "message": f"Python error managing script: {str(e)}",
        }


@mcp_for_unity_tool(
    unity_target=None,
    group=None,
    description=(
        """Get manage_script capabilities (supported ops, limits, and guards).
    Returns:
        - ops: list of supported structured ops
        - text_ops: list of supported text ops
        - max_edit_payload_bytes: server edit payload cap
        - guards: header/using guard enabled flag"""
    ),
    annotations=ToolAnnotations(
        title="Manage Script Capabilities",
        readOnlyHint=True,
    ),
)
async def manage_script_capabilities(ctx: Context) -> dict[str, Any]:
    await ctx.info("Processing manage_script_capabilities")
    try:
        # Keep in sync with server/Editor ManageScript implementation
        ops = [
            "replace_class", "delete_class", "replace_method", "delete_method",
            "insert_method", "anchor_insert", "anchor_delete", "anchor_replace"
        ]
        text_ops = ["replace", "prepend", "append", "delete_range", "bulk"]
        max_edit_payload_bytes = 4 * 1024 * 1024
        guards = {"using_guard": True}
        extras = {
            "get_sha": True,
            "hashline_protocol": "hashline-blake2s-v1",
            "partial_read": True,
            "auto_read_default": True,
            "legacy_shape_error": "E_LEGACY_SHAPE",
            "fuzzy_relocation": False,
        }
        return {"success": True, "data": {
            "ops": ops,
            "text_ops": text_ops,
            "max_edit_payload_bytes": max_edit_payload_bytes,
            "guards": guards,
            "extras": extras,
        }}
    except Exception as e:
        return {"success": False, "error": f"capabilities error: {e}"}


@mcp_for_unity_tool(
    unity_target="manage_script",
    description="Get SHA256 and basic metadata for a Unity C# script without returning file contents. Requires uri (script path under Assets/ or mcpforunity://path/Assets/... or file://...).",
    annotations=ToolAnnotations(
        title="Get SHA",
        readOnlyHint=True,
    ),
)
async def get_sha(
    ctx: Context,
    uri: Annotated[str, "URI of the script to edit under Assets/ directory, mcpforunity://path/Assets/... or file://... or Assets/..."],
) -> dict[str, Any]:
    unity_instance = await get_unity_instance_from_context(ctx)
    await ctx.info(
        f"Processing get_sha: {uri} (unity_instance={unity_instance or 'default'})")
    try:
        name, directory = _split_uri(uri)
        params = {"action": "get_sha", "name": name, "path": directory}
        resp = await send_with_unity_instance(
            transport.legacy.unity_connection.async_send_command_with_retry,
            unity_instance,
            "manage_script",
            params,
        )
        if isinstance(resp, dict) and resp.get("success"):
            data = resp.get("data", {})
            minimal = {"sha256": data.get(
                "sha256"), "lengthBytes": data.get("lengthBytes")}
            return {"success": True, "data": minimal}
        return resp if isinstance(resp, dict) else {"success": False, "message": str(resp)}
    except Exception as e:
        return {"success": False, "message": f"get_sha error: {e}"}
