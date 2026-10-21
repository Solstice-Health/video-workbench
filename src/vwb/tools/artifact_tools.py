"""General interactive HTML review surfaces, separate from the video composition schema."""
from typing import Any

from vwb.artifacts import service
from vwb.tools.base import Tool, ToolContext, ToolResult, parse_json_arg, schema

GUIDE = """Write self-contained UTF-8 HTML in work/, then artifact_publish(html_path, title, items, assets).
Each item is {id, label, asset_path?}; asset_path and assets entries are request-relative public files
(e.g. work/studio/output.png or renders/short-01/v001/preview.mp4). Only listed assets are served.
Use <img data-vwb-asset="work/studio/output.png" data-vwb-item="shot-1">, or
<video controls preload="metadata" data-vwb-asset="renders/short.mp4" data-vwb-item="shot-1"></video>.
For custom interactions use window.VWB.assets[path] and VWB.select({item_id:'shot-1',
region:{x:0.1,y:0.2,width:0.3,height:0.4},time_s:2.5,text:'Suggested feedback'}).
Region values are normalized 0–1 relative to the original reviewed item. Video times are seconds.
Clicking data-vwb-item stages that item (and the current media time); nested item IDs should be avoided.
The user reviews the selection in Workbench and explicitly sends their feedback. HTML cannot submit chat,
access host APIs, navigate the host, or load remote resources. Inline JS/CSS and listed local resources work.
Use buttons, labels, keyboard controls and lazy loading for large galleries. Publish the same artifact_id
for a new immutable HTML revision. Changed source assets require republishing. Read artifact_feedback
for item/revision/region/time metadata; submitted feedback also arrives as a normal queued chat message.
"""


def artifact_publish(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    row = service.publish(ctx.ws, html=a["html_path"], title=a["title"], actor=ctx.agent,
                          artifact_id=a.get("artifact_id"), items=parse_json_arg(a.get("items")),
                          assets=parse_json_arg(a.get("assets")))
    return ToolResult.json({"artifact": service.public(row), "note": "Published in the canvas. " + GUIDE},
                           artifacts=[row["sha256"]])


def artifact_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    return ToolResult.json({"artifacts": [service.public(row) for row in service.listing(ctx.ws)], "guide": GUIDE})


def artifact_feedback(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    return ToolResult.json(service.feedback(ctx.ws, a["artifact_id"], after=int(a.get("after", 0)),
                                            limit=int(a.get("limit", 100))))


TOOLS = [
    Tool("artifact_publish", "Publish any agent-authored HTML as an isolated interactive canvas tab with review "
         "items, charts, images, videos or custom controls. HTML in work/; explicit assets use request-relative paths. "
         "Use data-vwb-item and data-vwb-asset attributes or VWB.select for item/region/time feedback. "
         "The host requires the user's Send feedback action. artifact_list returns the bridge guide.",
         schema({"html_path": {"type": "string"}, "title": {"type": "string"}, "artifact_id": {"type": "string"},
                 "items": {"type": "array", "items": {"type": "object", "properties": {
                     "id": {"type": "string"}, "label": {"type": "string"}, "asset_path": {"type": "string"}},
                     "required": ["id", "label"], "additionalProperties": False}},
                 "assets": {"type": "array", "items": {"type": "string"}}}, ["html_path", "title"]),
         artifact_publish, "artifact.publish", "artifacts"),
    Tool("artifact_list", "List published HTML review canvases and read the authoring/feedback bridge guide.",
         schema({}), artifact_list, "tool", "artifacts"),
    Tool("artifact_feedback", "Read user-confirmed canvas feedback with stable item IDs, artifact revisions, "
         "regions and media times. after is the previous cursor; feedback also enters normal chat.",
         schema({"artifact_id": {"type": "string"}, "after": {"type": "integer"}, "limit": {"type": "integer"}},
                ["artifact_id"]), artifact_feedback, "tool", "artifacts"),
]
