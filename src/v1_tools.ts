/** Generic tools. Always require Blender Live; no automatic headless fallback for writes. */
import type { McpServer } from '@modelcontextprotocol/server';
import * as z from 'zod/v4';

type Execute = (action: string, payload: Record<string, unknown>) => Promise<unknown>;
const vec = z.tuple([z.number().finite(), z.number().finite(), z.number().finite()]);
const unitColor = z.tuple([
  z.number().finite().min(0).max(1),
  z.number().finite().min(0).max(1),
  z.number().finite().min(0).max(1),
]);
const lightOptions = z.object({
  energy: z.number().finite().min(0).max(100000).optional(),
  color: unitColor.optional(),
  location: vec.optional(),
  size: z.number().finite().min(0.01).max(1000).optional(),
  target: vec.optional(),
  rotation_degrees: vec.optional(),
});
function response(data: unknown) {
  return { content: [{ type: 'text' as const, text: JSON.stringify(data, null, 2) }] };
}
function failure(e: unknown) {
  return { isError: true as const, content: [{ type: 'text' as const, text: e instanceof Error ? e.message : String(e) }] };
}
export function registerV1Tools(server: McpServer, execute: Execute): void {
  server.registerTool('list_scene_objects', {
    description: 'Read object names/types, world positions and dimensions in the CURRENT open Blender scene. Paginated. Read-only.',
    inputSchema: z.object({ offset: z.number().int().min(0).default(0), limit: z.number().int().min(1).max(75).default(50) }),
  }, async (args) => { try { return response(await execute('list_scene_objects', args)); } catch (e) { return failure(e); } });

  server.registerTool('get_object', {
    description: 'Inspect ONE named Blender object, including world-space bounding box and light properties when applicable. Read-only.',
    inputSchema: z.object({ name: z.string().min(1).max(150) }),
  }, async (args) => { try { return response(await execute('get_object', args)); } catch (e) { return failure(e); } });

  server.registerTool('list_lights', {
    description: 'Read all light names, types, energy, color, location, rotation and area size in open Blender scene (paginated). Read-only.',
    inputSchema: z.object({ offset: z.number().int().min(0).default(0), limit: z.number().int().min(1).max(100).default(75) }),
  }, async (args) => { try { return response(await execute('list_lights', args)); } catch (e) { return failure(e); } });

  server.registerTool('set_light_properties', {
    description: 'Modify ONLY the specified existing light in the OPEN Blender scene. No auto-save. Provide one or more properties. The AI host must ask the user before edits.',
    inputSchema: z.object({ name: z.string().min(1).max(150), ...lightOptions.shape }).refine(v =>
      Object.keys(v).some(k => k !== 'name'), 'No properties to update'
    ).refine(v => !(v.target && v.rotation_degrees), 'Use target OR rotation_degrees'),
  }, async (args) => { try { return response(await execute('set_light_properties', args)); } catch (e) { return failure(e); } });

  server.registerTool('set_camera_pose', {
    description: 'Restore/set exact camera world position and Euler rotation in degrees plus focal length; used for reliable rollback. Requires active camera. No auto-save.',
    inputSchema: z.object({ position: vec, rotation_degrees: vec, lens_mm: z.number().finite().min(1).max(500) }),
  }, async (args) => { try { return response(await execute('set_camera_pose', args)); } catch (e) { return failure(e); } });

  server.registerTool('create_light', {
    description: 'EXPLICIT USER APPROVAL REQUIRED: create a new light in the open Blender scene (never auto-save). Not used by automatic v1 agent.',
    inputSchema: z.object({
      name: z.string().min(1).max(100),
      light_type: z.enum(['AREA', 'POINT', 'SPOT', 'SUN']),
      location: vec,
      energy: z.number().finite().min(0).max(100000),
      color: unitColor.optional(),
      size: z.number().finite().min(0.01).max(1000).optional(),
      target: vec.optional(),
    }),
  }, async (args) => { try { return response(await execute('create_light', args)); } catch (e) { return failure(e); } });
}
