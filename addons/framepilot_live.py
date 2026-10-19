bl_info = {
    "name": "FramePilot Live",
    "author": "FramePilot",
    "version": (1, 0, 0),
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
from mathutils import Vector, Matrix, Euler

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
        "rotation_degrees": _vec([math.degrees(v) for v in camera.matrix_world.to_euler()]),
        "parent": camera.parent.name if camera.parent else None,
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


def _world_bbox(obj):
    return [_vec(obj.matrix_world @ Vector(corner)) for corner in obj.bound_box]


def _object_info(obj):
    info = {
        "name": obj.name, "type": obj.type,
        "location": _vec(obj.matrix_world.translation),
        "dimensions": _vec(obj.dimensions),
        "rotation_degrees": _vec([math.degrees(v) for v in obj.matrix_world.to_euler()]),
        "parent": obj.parent.name if obj.parent else None,
        "bounding_box_world": _world_bbox(obj),
    }
    if obj.type == 'LIGHT':
        light = obj.data
        info.update({"energy": float(light.energy), "color": _vec(light.color),
                     "light_type": light.type,
                     "size": float(light.size) if light.type == 'AREA' else None})
    if obj.type == 'MESH':
        info["materials"] = [m.name for m in obj.data.materials if m is not None][:15]
    return info


def _page(values, payload, maximum):
    offset = int(payload.get("offset", 0))
    limit = int(payload.get("limit", maximum))
    if offset < 0 or not 1 <= limit <= maximum:
        raise ValueError("Invalid pagination")
    return {"mode": "live", "total": len(values), "offset": offset,
            "items": values[offset:offset+limit],
            "next_offset": offset+limit if offset+limit < len(values) else None}


def _list_scene_objects(payload):
    scene = bpy.context.scene
    values = [{"name": ob.name, "type": ob.type,
               "location": _vec(ob.matrix_world.translation), "dimensions": _vec(ob.dimensions)}
              for ob in sorted(scene.objects, key=lambda x: x.name)]
    return _page(values, payload, 75)


def _get_object(payload):
    name = payload.get("name")
    ob = bpy.context.scene.objects.get(name) if isinstance(name, str) else None
    if ob is None:
        raise ValueError("Object not found in current scene: " + str(name))
    return {"mode": "live", "object": _object_info(ob)}


def _list_lights(payload):
    scene = bpy.context.scene
    lights = [_object_info(ob) for ob in sorted(scene.objects, key=lambda x: x.name)
              if ob.type == 'LIGHT']
    return _page(lights, payload, 100)


def _finite_vec(values, label):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError(label + " must be three numbers")
    numbers = [float(x) for x in values]
    if not all(math.isfinite(x) for x in numbers):
        raise ValueError(label + " values must be finite")
    return Vector(numbers)


def _set_light_properties(payload):
    name = payload.get("name")
    obj = bpy.context.scene.objects.get(name) if isinstance(name, str) else None
    if obj is None or obj.type != 'LIGHT':
        raise ValueError("Light not found in active scene: " + str(name))
    if obj.data.users > 1:
        raise ValueError("Light data is shared by multiple objects; make it single-user before editing")
    allowed = {"name", "energy", "color", "location", "size", "target", "rotation_degrees"}
    if set(payload) - allowed or len(payload) < 2:
        raise ValueError("Unknown properties or no changes supplied")
    if "target" in payload and "rotation_degrees" in payload:
        raise ValueError("Choose either target or rotation_degrees")
    # Validate all values BEFORE any mutations, so failed input cannot partially edit scene.
    edits = {}
    if "energy" in payload:
        value = float(payload["energy"])
        if not math.isfinite(value) or not 0 <= value <= 100000:
            raise ValueError("energy out of range")
        edits["energy"] = value
    if "color" in payload:
        value = _finite_vec(payload["color"], "color")
        if any(not 0 <= c <= 1 for c in value):
            raise ValueError("color channels must be in [0,1]")
        edits["color"] = value
    if "size" in payload:
        value = float(payload["size"])
        if obj.data.type != 'AREA' or not math.isfinite(value) or not 0.01 <= value <= 1000:
            raise ValueError("size supported only for AREA lights")
        edits["size"] = value
    if "location" in payload:
        edits["location"] = _finite_vec(payload["location"], "location")
    if "rotation_degrees" in payload:
        edits["rotation_degrees"] = _finite_vec(payload["rotation_degrees"], "rotation_degrees")
    if "target" in payload:
        target = _finite_vec(payload["target"], "target")
        origin = edits.get("location", obj.matrix_world.translation)
        direction = target - origin
        if direction.length < 0.001:
            raise ValueError("target must differ from light position")
        edits["rotation"] = direction.to_track_quat('-Z', 'Y').to_euler()
    if "energy" in edits: obj.data.energy = edits["energy"]
    if "color" in edits: obj.data.color = edits["color"]
    if "size" in edits: obj.data.size = edits["size"]
    # Transform edits use WORLD coordinates, including parented lights.
    if "location" in edits or "rotation_degrees" in edits or "rotation" in edits:
        loc, quat, scale = obj.matrix_world.decompose()
        loc = edits.get("location", loc)
        if "rotation_degrees" in edits:
            quat = Euler([math.radians(x) for x in edits["rotation_degrees"]]).to_quaternion()
        elif "rotation" in edits:
            quat = edits["rotation"].to_quaternion()
        obj.matrix_world = (Matrix.Translation(loc) @ quat.to_matrix().to_4x4()
                            @ Matrix.Diagonal((scale.x, scale.y, scale.z, 1.0)))
    bpy.context.view_layer.update()
    _redraw()
    return {"mode": "live", "saved": False, "light": _object_info(obj)}


def _set_camera_pose(payload):
    cam = bpy.context.scene.camera
    if cam is None: raise ValueError("No active camera to restore")
    pos = _finite_vec(payload["position"], "position")
    rot = _finite_vec(payload["rotation_degrees"], "rotation_degrees")
    lens = float(payload["lens_mm"])
    if not math.isfinite(lens) or not 1 <= lens <= 500:
        raise ValueError("Invalid lens")
    cam.matrix_world = (Matrix.Translation(pos) @
                        Euler([math.radians(x) for x in rot]).to_matrix().to_4x4())
    cam.data.lens = lens
    bpy.context.view_layer.update()
    _redraw()
    return {"mode": "live", "saved": False, "camera": _camera_info(cam)}


def _create_light(payload):
    name = payload["name"]
    typ = payload["light_type"]
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError("Invalid name")
    if typ not in {'AREA','POINT','SPOT','SUN'}:
        raise ValueError("Invalid light type")
    position = _finite_vec(payload["location"], "location")
    energy = float(payload["energy"])
    if not math.isfinite(energy) or not 0 <= energy <= 100000:
        raise ValueError("Invalid light energy")
    color = _finite_vec(payload.get("color", [1, 1, 1]), "color")
    if any(not 0 <= c <= 1 for c in color):
        raise ValueError("Invalid light color")
    size = float(payload.get("size", 1.0))
    if not math.isfinite(size) or not 0.01 <= size <= 1000:
        raise ValueError("Invalid light size")
    target = payload.get("target")
    rotation = None
    if target is not None:
        direction = _finite_vec(target, "target") - position
        if direction.length < 0.001: raise ValueError("Target equals position")
        rotation = direction.to_track_quat('-Z','Y').to_euler()
    if bpy.context.scene.objects.get(name):
        raise ValueError("Object name already exists in scene")
    data = bpy.data.lights.new(name, typ)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = position
    if rotation is not None: obj.rotation_euler = rotation
    obj.data.energy = energy
    obj.data.color = color
    if typ == 'AREA': obj.data.size = size
    bpy.context.view_layer.update()
    _redraw()
    return {"mode": "live", "saved": False, "light": _object_info(obj)}


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
    camera.matrix_world = (Matrix.Translation(position) @
                           direction.to_track_quat('-Z', 'Y').to_matrix().to_4x4())
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
        "list_scene_objects": _list_scene_objects,
        "get_object": _get_object,
        "list_lights": _list_lights,
        "set_light_properties": _set_light_properties,
        "set_camera_pose": _set_camera_pose,
        "create_light": _create_light,
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
            if action not in {"inspect", "set_camera", "set_light_energy", "render", "list_scene_objects", "get_object", "list_lights", "set_light_properties", "set_camera_pose", "create_light"}:
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
