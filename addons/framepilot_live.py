bl_info = {
    "name": "FramePilot Live",
    "author": "FramePilot",
    "version": (0, 2, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > FramePilot",
    "description": "Expose a running Blender scene to a local FramePilot MCP server",
    "category": "3D View",
}

import json
import math
import os
import queue
import secrets
import socket
import threading
import traceback
from pathlib import Path

import bpy
from bpy.props import StringProperty
from mathutils import Vector

# The socket thread never calls bpy. A Blender timer executes ALL scene changes.
_jobs = queue.Queue()
_server_socket = None
_server_thread = None
_stop_event = threading.Event()
_connection_path = None
_token = None
_project_root = None
_port = None
_MAX_COMMAND = 64 * 1024


def _vec(values):
    return [round(float(v), 4) for v in values]


def _camera_info(camera):
    if camera is None:
        return None
    return {
        "name": camera.name,
        "position": _vec(camera.matrix_world.translation),
        "rotation_degrees": _vec([math.degrees(v) for v in camera.rotation_euler]),
        "lens_mm": camera.data.lens,
    }


def _inspect_scene(_payload):
    scene = bpy.context.scene
    objects = []
    for obj in list(scene.objects)[:75]:
        item = {
            "name": obj.name,
            "type": obj.type,
            "location": _vec(obj.matrix_world.translation),
            "dimensions": _vec(obj.dimensions),
        }
        if obj.type == 'LIGHT':
            item["energy"] = obj.data.energy
        objects.append(item)
    return {
        "mode": "live",
        "scene": scene.name,
        "blend_file": bpy.data.filepath or "(unsaved)",
        "object_count": len(scene.objects),
        "objects": objects,
        "truncated": len(scene.objects) > len(objects),
        "camera": _camera_info(scene.camera),
        "render_engine": scene.render.engine,
    }


def _redraw():
    wm = bpy.context.window_manager
    if not wm:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _set_camera(payload):
    scene = bpy.context.scene
    position = Vector(payload["position"])
    target = Vector(payload["target"])
    lens = float(payload.get("lens_mm", 50))
    if len(position) != 3 or len(target) != 3 or not (18 <= lens <= 120):
        raise ValueError("Invalid camera parameters")
    if not all(math.isfinite(x) for x in (*position, *target, lens)):
        raise ValueError("Camera values must be finite")
    direction = target - position
    if direction.length < 0.001:
        raise ValueError("Camera position and target must differ")

    camera = scene.camera
    if camera is None:
        camera_data = bpy.data.cameras.new("FramePilot Camera")
        camera = bpy.data.objects.new("FramePilot Camera", camera_data)
        scene.collection.objects.link(camera)
        scene.camera = camera
    camera.location = position
    camera.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    camera.data.lens = lens
    camera.data.type = 'PERSP'
    bpy.context.view_layer.update()
    _redraw()
    return {"mode": "live", "saved": False, "camera": _camera_info(camera)}


def _set_light_energy(payload):
    energy = float(payload["energy"])
    if not math.isfinite(energy) or not (0 <= energy <= 10000):
        raise ValueError("energy must be between 0 and 10000")
    name = payload.get("light_name")
    light = bpy.context.scene.objects.get(name) if isinstance(name, str) and name else None
    if light is None and not name:
        light = bpy.context.view_layer.objects.active
    if light is None or light.type != 'LIGHT':
        raise ValueError("Select a light in Blender or pass light_name of a light object")
    light.data.energy = energy
    bpy.context.view_layer.update()
    _redraw()
    return {"mode": "live", "saved": False, "light": light.name, "energy": light.data.energy}


def _render_preview(payload):
    scene = bpy.context.scene
    if scene.camera is None:
        raise ValueError("Scene has no camera")
    # Never trust a user-supplied destination path. Only write into project's runtime folder.
    path = os.path.join(_project_root, "runtime", "preview.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    original = (
        scene.render.resolution_x,
        scene.render.resolution_y,
        scene.render.resolution_percentage,
        scene.render.image_settings.file_format,
        scene.render.filepath,
        scene.render.engine,
    )
    original_samples = scene.cycles.samples if scene.render.engine == 'CYCLES' else None
    try:
        scene.render.resolution_x = 480
        scene.render.resolution_y = 270
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'
        scene.render.filepath = path
        available = {item.identifier for item in scene.render.bl_rna.properties['engine'].enum_items}
        if 'BLENDER_EEVEE' in available:
            scene.render.engine = 'BLENDER_EEVEE'
        elif 'BLENDER_EEVEE_NEXT' in available:
            scene.render.engine = 'BLENDER_EEVEE_NEXT'
        elif scene.render.engine == 'CYCLES':
            scene.cycles.samples = 16
        bpy.ops.render.render(write_still=True)
        if not os.path.isfile(path):
            raise RuntimeError("Blender did not create a PNG preview")
        return {"mode": "live", "preview": path, "width": 480, "height": 270}
    finally:
        (scene.render.resolution_x,
         scene.render.resolution_y,
         scene.render.resolution_percentage,
         scene.render.image_settings.file_format,
         scene.render.filepath,
         scene.render.engine) = original
        if original_samples is not None:
            scene.cycles.samples = original_samples


def _execute(action, payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    handlers = {
        "inspect": _inspect_scene,
        "set_camera": _set_camera,
        "set_light_energy": _set_light_energy,
        "render": _render_preview,
    }
    fn = handlers.get(action)
    if fn is None:
        raise ValueError("Unsupported FramePilot action: " + str(action))
    return fn(payload)


def _process_jobs():
    if _stop_event.is_set():
        return None
    # Limit non-render work per tick so normal viewport interaction stays responsive.
    for _ in range(3):
        try:
            job = _jobs.get_nowait()
        except queue.Empty:
            break
        if job["cancelled"]:
            job["event"].set()
            continue
        try:
            job["result"] = {"ok": True, "result": _execute(job["action"], job["payload"])}
        except Exception as exc:
            job["result"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            traceback.print_exc()
        finally:
            job["event"].set()
    return 0.1


def _serve_one(conn):
    conn.settimeout(10.0)
    with conn:
        try:
            # One newline-delimited JSON request per connection.
            with conn.makefile("rb") as incoming:
                line = incoming.readline(_MAX_COMMAND + 1)
            if not line or len(line) > _MAX_COMMAND or not line.endswith(b"\n"):
                raise ValueError("Missing or oversized request")
            request = json.loads(line)
            if not isinstance(request, dict) or not secrets.compare_digest(
                str(request.get("token", "")), _token
            ):
                raise PermissionError("Invalid FramePilot token")
            action = request.get("action")
            if action not in {"inspect", "set_camera", "set_light_energy", "render"}:
                raise ValueError("Unsupported action")
            job = {
                "action": action,
                "payload": request.get("payload", {}),
                "event": threading.Event(),
                "result": None,
                "cancelled": False,
            }
            _jobs.put(job)
            # Rendering may block Blender's main thread while producing an image.
            if not job["event"].wait(245.0):
                job["cancelled"] = True
                raise TimeoutError("Blender has not completed the action")
            response = job["result"] or {"ok": False, "error": "No result"}
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        except (OSError, BrokenPipeError):
            pass


def _accept_loop():
    while not _stop_event.is_set():
        try:
            connection, _address = _server_socket.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        _serve_one(connection)


def _resolve_root(context):
    manual = context.window_manager.framepilot_project_dir.strip()
    if manual:
        root = Path(bpy.path.abspath(manual)).expanduser().resolve()
    elif bpy.data.filepath:
        source = Path(bpy.data.filepath).resolve()
        root = source.parent
        if root.name == "runtime" and source.name == "session.blend":
            root = root.parent
    else:
        raise ValueError("Save the .blend file or choose your FramePilot project folder")
    if not (root / "src" / "server.ts").is_file():
        raise ValueError("Choose the FramePilot folder containing src/server.ts")
    return root


def _start_server(context):
    global _server_socket, _server_thread, _connection_path, _token, _project_root, _port
    if _server_socket is not None:
        return
    root = _resolve_root(context)
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))  # Loopback only, random port.
        sock.listen(2)
        sock.settimeout(0.5)
        port = sock.getsockname()[1]
        token = secrets.token_urlsafe(32)
        config = runtime / "framepilot-live.json"
        temp = runtime / "framepilot-live.json.tmp"
        # Only the user's account should be able to read the auth token.
        fd = os.open(str(temp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"host": "127.0.0.1", "port": port, "token": token}, stream)
        os.chmod(temp, 0o600)
        os.replace(temp, config)
        _stop_event.clear()
        _server_socket = sock
        _connection_path = config
        _token = token
        _project_root = str(root)
        _port = port
        if not bpy.app.timers.is_registered(_process_jobs):
            bpy.app.timers.register(_process_jobs, first_interval=0.1, persistent=True)
        _server_thread = threading.Thread(target=_accept_loop, name="FramePilot Live", daemon=True)
        _server_thread.start()
    except Exception:
        sock.close()
        raise


def _stop_server():
    global _server_socket, _server_thread, _connection_path, _token, _project_root, _port
    _stop_event.set()
    if _server_socket is not None:
        _server_socket.close()
        _server_socket = None
    if _server_thread is not None:
        _server_thread.join(timeout=1.0)
        _server_thread = None
    if bpy.app.timers.is_registered(_process_jobs):
        bpy.app.timers.unregister(_process_jobs)
    while True:
        try:
            job = _jobs.get_nowait()
            job["cancelled"] = True
            job["event"].set()
        except queue.Empty:
            break
    if _connection_path is not None:
        try:
            _connection_path.unlink(missing_ok=True)
        except OSError:
            pass
    _connection_path = None
    _token = None
    _project_root = None
    _port = None


class FRAMEPILOT_OT_start(bpy.types.Operator):
    bl_idname = "framepilot.start_live"
    bl_label = "Start Live Bridge"
    bl_description = "Allow the local FramePilot MCP server to control this open Blender scene"

    def execute(self, context):
        try:
            _start_server(context)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "FramePilot Live connected (localhost)")
        return {'FINISHED'}


class FRAMEPILOT_OT_stop(bpy.types.Operator):
    bl_idname = "framepilot.stop_live"
    bl_label = "Stop Live Bridge"

    def execute(self, context):
        _stop_server()
        return {'FINISHED'}


class FRAMEPILOT_PT_panel(bpy.types.Panel):
    bl_label = "FramePilot Live"
    bl_idname = "FRAMEPILOT_PT_live"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'FramePilot'

    def draw(self, context):
        layout = self.layout
        active = _server_socket is not None
        layout.label(text="Connected" if active else "Disconnected", icon='LINKED' if active else 'UNLINKED')
        if active:
            layout.label(text="Localhost only · port " + str(_port))
            layout.operator("framepilot.stop_live", icon='CANCEL')
            layout.label(text="Edits affect the open scene; save manually")
        else:
            layout.prop(context.window_manager, "framepilot_project_dir", text="Project")
            layout.operator("framepilot.start_live", icon='PLAY')


_CLASSES = (FRAMEPILOT_OT_start, FRAMEPILOT_OT_stop, FRAMEPILOT_PT_panel)


def register():
    bpy.types.WindowManager.framepilot_project_dir = StringProperty(
        name="FramePilot Project", subtype='DIR_PATH',
        description="Leave empty if your .blend file is in the FramePilot project folder",
    )
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    _stop_server()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.framepilot_project_dir
