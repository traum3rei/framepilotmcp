"""Optional one-time lighting test for the FramePilot demo scene.

Blender: Scripting > open this file > Run Script. No autosave. Idempotent: repeated
runs update the same three lights rather than making duplicates.
This DOES edit the open Blender scene (Undo may not capture changes by script;
reopen saved file or delete the named lights to revert).
"""
import bpy
from mathutils import Vector

scene = bpy.context.scene
expected = {"Hall floor", "Left wall", "Right wall", "The Watcher / iris"}
missing = expected - set(scene.objects.keys())
if missing:
    raise RuntimeError("Not the FramePilot demo scene; missing: " + ", ".join(sorted(missing)))


def ensure_area_light(name, position, target, energy, color, size):
    obj = scene.objects.get(name)
    if obj is not None and obj.type != "LIGHT":
        raise RuntimeError(f"Name conflict: {name} is not a light")
    if obj is None:
        data = bpy.data.lights.new(name, type="AREA")
        obj = bpy.data.objects.new(name, data)
        scene.collection.objects.link(obj)
    if obj.data.type != "AREA":
        raise RuntimeError(f"Existing {name} is not an area light")
    obj.location = Vector(position)
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    obj.data.energy = energy
    obj.data.color = color
    obj.data.shape = "DISK"
    obj.data.size = size
    print(f"[FramePilot] Lit: {name} = {energy} W")


# Broad soft grazing illumination of the existing pillars and side walls.
ensure_area_light(
    "FramePilot / Blue corridor fill", (-1.25, 2.0, 3.65),
    (-2.8, 4.3, 1.7), 1350, (0.45, 0.64, 1.0), 5.0,
)
ensure_area_light(
    "FramePilot / Warm corridor fill", (1.25, 3.5, 3.65),
    (2.8, 5.0, 1.7), 1150, (1.0, 0.48, 0.33), 5.0,
)
ensure_area_light(
    "FramePilot / Back pillar rim", (0.0, 7.6, 3.7),
    (0.0, 4.6, 1.6), 1250, (0.36, 0.52, 1.0), 4.0,
)

bpy.context.view_layer.update()
for screen in bpy.data.screens:
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()
print("[FramePilot] Corridor lights ready (unsaved scene). Render to inspect; tune energy as needed.")
