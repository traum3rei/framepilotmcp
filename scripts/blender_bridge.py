"""Invoked by Blender --background <blendfile> --python scripts/blender_bridge.py -- action json.
Never print result data except on the FRAMEPILOT_JSON line (parsed by Node).
"""
import bpy
import json
import sys
from mathutils import Vector


def vec(v):
    return [round(float(x), 4) for x in v]


def camera_info(camera):
    if camera is None:
        return None
    return {
        "name": camera.name,
        "position": vec(camera.matrix_world.translation),
        "rotation_degrees": vec([float(x) * 180.0 / 3.141592653589793 for x in camera.rotation_euler]),
        "lens_mm": camera.data.lens,
    }


def inspect_scene():
    scene = bpy.context.scene
    objects = []
    for obj in list(scene.objects)[:75]:
        objects.append({
            "name": obj.name,
            "type": obj.type,
            "location": vec(obj.matrix_world.translation),
            "dimensions": vec(obj.dimensions),
        })
    return {
        "scene": scene.name,
        "objects": objects,
        "object_count": len(scene.objects),
        "truncated": len(scene.objects) > len(objects),
        "camera": camera_info(scene.camera),
        "render_engine": scene.render.engine,
    }


def set_camera(payload):
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        bpy.ops.object.camera_add()
        camera = bpy.context.object
        scene.camera = camera

    position = Vector(payload["position"])
    target = Vector(payload["target"])
    direction = target - position
    if direction.length < 0.001:
        raise ValueError("Camera position and target must be different")

    camera.location = position
    camera.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    camera.data.lens = float(payload["lens_mm"])
    camera.data.type = 'PERSP'
    bpy.context.view_layer.update()
    bpy.ops.wm.save_as_mainfile(filepath=payload["workingScene"], check_existing=False)
    return {"saved_working_copy": payload["workingScene"], "camera": camera_info(camera)}

def set_light_energy(payload):
    scene = bpy.context.scene

    light_name = payload["light_name"]
    energy = float(payload["energy"])

    light = scene.objects.get(light_name)

    if light is None or light.type != 'LIGHT':
        raise ValueError(f"Light '{light_name}' not found")

    light.data.energy = energy

    bpy.ops.wm.save_as_mainfile(
        filepath=payload["workingScene"],
        check_existing=False
    )

    return {
        "light": light.name,
        "energy": light.data.energy
    }

def render_preview(payload):
    scene = bpy.context.scene
    if scene.camera is None:
        raise ValueError("Scene has no camera; call set_camera first")
    # Preview-only settings: the original .blend is never saved with these.
    scene.render.resolution_x = 480
    scene.render.resolution_y = 270
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = payload["previewPath"]
    scene.render.film_transparent = False
    available = {item.identifier for item in scene.render.bl_rna.properties['engine'].enum_items}
    if 'BLENDER_EEVEE' in available:
        scene.render.engine = 'BLENDER_EEVEE'
    elif 'BLENDER_EEVEE_NEXT' in available:
        scene.render.engine = 'BLENDER_EEVEE_NEXT'
    elif scene.render.engine == 'CYCLES':
        scene.cycles.samples = 16
    bpy.ops.render.render(write_still=True)
    return {"preview": payload["previewPath"], "width": 480, "height": 270}


def main():
    args = sys.argv[sys.argv.index('--') + 1:]
    action, json_payload = args
    payload = json.loads(json_payload)
    if action == 'inspect':
        result = inspect_scene()
    elif action == 'set_camera':
        result = set_camera(payload)
    elif action == 'render':
        result = render_preview(payload)
    elif action == 'set_light_energy':
        result = set_light_energy(payload)
    else:
        raise ValueError(f"Unknown action: {action}")
    print('FRAMEPILOT_JSON:' + json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
