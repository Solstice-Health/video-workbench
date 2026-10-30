"""Registry-routed native After Effects tools. Human consent is configured only in UI."""

from vwb.canvas.renderers import after_effects_target
from vwb.desktop.after_effects import job_status, launch, settings_status
from vwb.tools.base import Tool, ToolResult, schema


def _runs_dir(ctx):
    service = ctx.state.get('integration_service')
    bridge = getattr(service, 'after_effects', None)
    return getattr(bridge, 'runs_dir', None)


def status(ctx, args):
    host = settings_status(_runs_dir(ctx))
    return ToolResult.json({**host, 'workspace_path': str(ctx.ws.work.resolve()), 'target': after_effects_target(host)})


def run(ctx, args):
    return ToolResult.json(launch(ctx.ws, ctx.agent, 'script', args, _runs_dir(ctx)))


def render(ctx, args):
    return ToolResult.json(launch(ctx.ws, ctx.agent, 'render', args, _runs_dir(ctx)))


def job(ctx, args):
    result = job_status(ctx.ws, args)
    value = ToolResult.json(result)
    value.is_error = result.get('status') in {'failed', 'timed_out', 'interrupted'}
    return value


TOOLS = [
    Tool('after_effects_status', 'Discover native After Effects installation, human enablement and unresolved script '
         'executions, plus the native authoring target workflow. Does not launch the app or verify Adobe licensing, '
         'script file access or macOS consent. Native JSX has desktop-user file/network access; it is not sandboxed.',
         schema({}), status, 'desktop.after_effects.status', 'creative'),
    Tool('after_effects_run', 'Run a UTF-8 .jsx script written with file_write, in native macOS After Effects. '
         'Requires a person to enable trusted local automation in Plugins and MCPs. Returns a durable background '
         'job; poll after_effects_job. Default isolated creates a separate saved project and refuses to discard '
         'unsaved user changes; project_path optionally opens a workspace project then saves a new copy. '
         'project_mode=current explicitly edits the current app project without automatic saving. '
         'Inside JSX, vwb.workPath/outputPath/projectPath are absolute paths, vwb.log(text) records progress, '
         'and vwb.result can hold JSON-compatible output. expected_outputs verifies newly created files. '
         'Return composition names and installed render/output templates in vwb.result for the render step. '
         'Completed isolated jobs return project.path; pass it to after_effects_render. This authors a native .aep, '
         'not a vwb.canvas/2 renderer swap or automatic conversion. '
         'Do not open dialogs or perform unbounded loops. Cancelling cannot forcibly stop JSX; press Esc in AE '
         'if needed. A timed-out script blocks further scripts until its receipt arrives or a person clears it.',
         schema({'script_path': {'type': 'string'},
                 'project_mode': {'type': 'string', 'enum': ['isolated', 'current']},
                 'project_path': {'type': 'string'},
                 'expected_outputs': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 32},
                 'timeout_s': {'type': 'number', 'minimum': 1, 'maximum': 1800}}, ['script_path']),
         run, 'desktop.after_effects.execute', 'creative'),
    Tool('after_effects_render', 'Render a saved workspace .aep/.aepx project using its installed aerender, '
         'without reusing or killing the user GUI app. Choose comp or render_queue_index. Output is a new '
         'workspace file; existing outputs are refused. Template names must match installed AE render/output '
         'templates. Returns a durable job and asserts nonempty output after success. Image sequences are '
         'not supported; a still requires equal start_frame/end_frame. end_frame is inclusive in AE. '
         'Poll after_effects_job, then optionally media_register(path=output.path) for video inspection/reuse. '
         'Native layers/effects remain in the .aep; rendered video import does not preserve their editability. '
         'Requires human-enabled native automation.',
         schema({'project_path': {'type': 'string'}, 'output_path': {'type': 'string'}, 'comp': {'type': 'string'},
                 'render_queue_index': {'type': 'integer', 'minimum': 1}, 'output_module': {'type': 'string'},
                 'render_settings': {'type': 'string'},
                 'start_frame': {'type': 'integer', 'minimum': 0},
                 'end_frame': {'type': 'integer', 'minimum': 0},
                 'timeout_s': {'type': 'number', 'minimum': 1, 'maximum': 3600}}, ['project_path', 'output_path']),
         render, 'desktop.after_effects.execute', 'creative'),
    Tool('after_effects_job', 'List/poll/cancel this request\'s native After Effects jobs. Logs use byte offsets. '
         'Jobs survive chat turns and are never automatically replayed. Script cancellation only stops waiting '
         'and can leave an uncertain execution; the GUI app is never force-killed. Renderer cancellation stops '
         'only the owned aerender process group. Completed outputs remain in the workspace. Native output review '
         'is not_evaluated; completed video renders include an optional media_register import_hint, not an automatic import.',
         schema({'job_id': {'type': 'string'}, 'cancel': {'type': 'boolean'},
                 'wait_s': {'type': 'number', 'minimum': 0, 'maximum': 30},
                 'stdout_offset': {'type': 'integer', 'minimum': 0},
                 'stderr_offset': {'type': 'integer', 'minimum': 0}}),
         job, 'desktop.after_effects.execute', 'creative'),
]
