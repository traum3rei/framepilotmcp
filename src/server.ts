import { McpServer } from '@modelcontextprotocol/server';
import { serveStdio } from '@modelcontextprotocol/server/stdio';
import * as z from 'zod/v4';
import { execFile } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const sourceScene = resolve(process.env.FRAMEPILOT_SCENE ?? resolve(root, 'scene.blend'));
const runtimeDir = resolve(root, 'runtime');
const workingScene = resolve(runtimeDir, 'session.blend');
const previewPath = resolve(runtimeDir, 'preview.png');
const bridge = resolve(root, 'scripts', 'blender_bridge.py');
const blender = process.env.BLENDER_PATH || (process.platform === 'darwin'
  ? '/Applications/Blender.app/Contents/MacOS/Blender'
  : 'blender');

function errorResult(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  return { isError: true as const, content: [{ type: 'text' as const, text: message }] };
}

async function runBlender(
  action: 'inspect' | 'set_camera' | 'render' | 'set_light_energy',
  payload: Record<string, unknown> = {},
) {
  if (!existsSync(sourceScene)) {
    throw new Error(`Keine Blender-Szene gefunden: ${sourceScene}. Erst npm run demo ausführen oder FRAMEPILOT_SCENE setzen.`);
  }
  await mkdir(runtimeDir, { recursive: true });
  // Use the session for camera changes; never overwrite the original .blend.
  const scene = existsSync(workingScene) ? workingScene : sourceScene;
  const args = [
    '--background',
    '--factory-startup', // Ignore user preferences and third-party add-ons for headless tasks.
    '--python-exit-code', '1', // Report Python exceptions as process failures.
    scene,
    '--python', bridge,
    '--', action, JSON.stringify({ ...payload, workingScene, previewPath }),
  ];
  let stdout: string;
  try {
    const result = await execFileAsync(blender, args, {
      cwd: root,
      timeout: 180_000,
      maxBuffer: 10 * 1024 * 1024,
      encoding: 'utf8',
    });
    stdout = result.stdout;
  } catch (e: unknown) {
    const detail = e as Error & { stderr?: string };
    throw new Error(`Blender konnte den Vorgang nicht abschließen. ${detail.message}\n${detail.stderr ?? ''}`);
  }
  const marker = 'FRAMEPILOT_JSON:';
  const line = stdout.split(/\r?\n/).filter(x => x.startsWith(marker)).at(-1);
  if (!line) throw new Error(`Kein gültiges Blender-Ergebnis. Ausgabe: ${stdout.slice(-3000)}`);
  const data: unknown = JSON.parse(line.slice(marker.length));
  return data;
}

// One Blender process at a time: avoid races between a camera change and a render.
let tail: Promise<unknown> = Promise.resolve();
function serialized<T>(job: () => Promise<T>): Promise<T> {
  const current = tail.then(job, job);
  tail = current.then(() => undefined, () => undefined);
  return current;
}

function createServer() {
  const server = new McpServer({ name: 'framepilot', version: '0.1.0' });

  server.registerTool('inspect_scene', {
    description: 'Inspect the current Blender scene, active camera, render engine and object positions. Does not modify anything.',
    inputSchema: z.object({}),
  }, async () => {
    try {
      const data = await serialized(() => runBlender('inspect'));
      return { content: [{ type: 'text' as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) { return errorResult(e); }
  });

  server.registerTool('set_light_energy', {
  description: 'Change the energy of a named light in Blender.',
  inputSchema: z.object({
    light_name: z.string().min(1),
    energy: z.number().min(0).max(10000),
  }),
}, async ({ light_name, energy }) => {
  try {
    const data = await serialized(() =>
      runBlender('set_light_energy', { light_name, energy })
    );

    return {
      content: [{
        type: 'text' as const,
        text: JSON.stringify(data, null, 2)
      }]
    };
  } catch (e) {
    return errorResult(e);
  }
});

  server.registerTool('set_camera', {
    description: 'Move the Blender camera to a 3D position, aim it at a 3D target and set its lens in millimeters. Edits a separate working copy, not the original scene. Coordinates in meters / Blender units.',
    inputSchema: z.object({
      position: z.tuple([z.number(), z.number(), z.number()])
        .describe('Camera location [x, y, z]'),
      target: z.tuple([z.number(), z.number(), z.number()])
        .describe('Point the camera looks at [x, y, z]'),
      lens_mm: z.number().min(18).max(120).default(50)
        .describe('Camera focal length in millimeters'),
    }),
  }, async ({ position, target, lens_mm }) => {
    try {
      const data = await serialized(() => runBlender('set_camera', { position, target, lens_mm }));
      return { content: [{ type: 'text' as const, text: JSON.stringify(data, null, 2) }] };
    } catch (e) { return errorResult(e); }
  });

  server.registerTool('render_preview', {
    description: 'Render a 480x270 PNG preview from the current camera. Returns the image so a vision-capable AI host can inspect the composition. Can take time for complex scenes.',
    inputSchema: z.object({}),
  }, async () => {
    try {
      // Keep reading the image inside the queue, so later renders cannot overwrite it.
      const png = await serialized(async () => {
        await runBlender('render');
        return readFile(previewPath);
      });
      return {
        content: [
          { type: 'text' as const, text: `Preview gerendert: ${previewPath} (480x270).` },
          { type: 'image' as const, data: png.toString('base64'), mimeType: 'image/png' },
        ],
      };
    } catch (e) { return errorResult(e); }
  });

  return server;
}

void serveStdio(createServer);
// MCP uses stdout for JSON-RPC. Logging MUST go to stderr.
console.error('FramePilot MCP ready (stdio).');
