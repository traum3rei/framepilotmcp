"""Create a tiny cinematic test scene. Run with Blender --background --python this_file -- <dest>."""
import bpy
import math
import os
import sys
from mathutils import Vector


def material(name, base, metallic=0, roughness=0.55, emission=None):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*base, 1)
    mat.use_nodes = True
    node = mat.node_tree.nodes.get('Principled BSDF')
    node.inputs['Base Color'].default_value = (*base, 1)
    node.inputs['Metallic'].default_value = metallic
    node.inputs['Roughness'].default_value = roughness
    if emission:
        node.inputs['Emission Color'].default_value = (*emission, 1)
        node.inputs['Emission Strength'].default_value = 6
    return mat


def cube(name, location, scale, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    ob = bpy.context.object
    ob.name = name
    ob.dimensions = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    ob.data.materials.append(mat)
    return ob


def area(name, location, power, color, size):
    data = bpy.data.lights.new(name, type='AREA')
    data.energy = power
    data.color = color
    data.shape = 'DISK'
    data.size = size
    ob = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(ob)
    ob.location = location
    ob.rotation_euler = (Vector((0, 0, 1.5)) - ob.location).to_track_quat('-Z', 'Y').to_euler()


bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
dark = material('Charcoal concrete', (0.075, 0.085, 0.10))
stone = material('Weathered pillars', (0.18, 0.18, 0.21))
glow = material('Red emissive iris', (0.65, 0.008, 0.02), emission=(0.9, 0.025, 0.02))
black = material('Black pupil', (0.0015, 0.0015, 0.002), roughness=0.18)
metal = material('Black metal', (0.05, 0.055, 0.065), metallic=0.85, roughness=0.25)

cube('Hall floor', (0, 0, -0.14), (7, 20, 0.28), dark)
cube('Left wall', (-3.7, 0, 2), (0.35, 20, 4.3), dark)
cube('Right wall', (3.7, 0, 2), (0.35, 20, 4.3), dark)
cube('Ceiling', (0, 0, 4.2), (7.8, 20, 0.25), dark)
cube('End wall', (0, 9.8, 2), (7.8, 0.5, 4.4), stone)
for y in [-7.5, -4.0, -0.5, 3, 6.5]:
    for side in (-1, 1):
        cube(f'Pillar {side} / {y}', (side * 2.8, y, 1.9), (0.7, 0.8, 3.8), stone)
        cube(f'Pillar foot {side} / {y}', (side * 2.8, y, .15), (1.0, 1.1, .3), metal)

# Suspended eye: sphere + pupil + metallic ring at back of corridor.
bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=1.05, location=(0, 5.6, 2.1))
eye = bpy.context.object
eye.name = 'The Watcher / iris'
eye.data.materials.append(glow)
bpy.ops.mesh.primitive_uv_sphere_add(segments=40, ring_count=20, radius=0.53, location=(0, 4.66, 2.1))
pupil = bpy.context.object
pupil.name = 'The Watcher / pupil'
pupil.data.materials.append(black)
bpy.ops.mesh.primitive_torus_add(major_radius=1.12, minor_radius=0.085, location=(0, 5.6, 2.1), rotation=(math.pi / 2, 0, 0))
ring = bpy.context.object
ring.name = 'The Watcher / ring'
ring.data.materials.append(metal)
area('Cold side light', (-2.6, -4, 3), 1200, (0.35, 0.5, 1.0), 4)
area('Warm eye light', (0, 4.2, 3.5), 800, (1.0, 0.12, 0.08), 3)

bpy.ops.object.camera_add(location=(3.0, -8.0, 1.9))
camera = bpy.context.object
camera.name = 'FramePilot Camera'
camera.rotation_euler = (Vector((0, 4.7, 1.9)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
camera.data.lens = 35
bpy.context.scene.camera = camera
world = bpy.context.scene.world
world.color = (0.08, 0.08, 0.08)
world.use_nodes = True
world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.02, 0.025, 0.04, 1.0)
world.node_tree.nodes['Background'].inputs['Strength'].default_value = 0.4
bpy.context.scene.render.resolution_x = 960
bpy.context.scene.render.resolution_y = 540
bpy.context.scene.render.resolution_percentage = 100
bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(sys.argv[sys.argv.index('--') + 1]))
print('FramePilot sample scene created.')
