/** One-time, idempotent patch of the existing FramePilot server; keeps custom MCP tools. */
import { readFile, writeFile, copyFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const path = resolve(root, 'src', 'server.ts');
let code = await readFile(path, 'utf8');
if (code.includes("import { runLive } from './live.js';") && code.includes('return runLive(action, ')) {
  console.log('FramePilot Live ist bereits aktiviert; keine Änderungen.');
  process.exit(0);
}
if (code.includes("import { runLive } from './live.js';") || code.includes('return runLive(action, ')) {
  throw new Error('Unvollständige Live-Änderung gefunden. Bitte src/server.ts prüfen; nichts verändert.');
}
const importAnchor = "import { McpServer } from '@modelcontextprotocol/server';";
const bodyAnchor = '  if (!existsSync(sourceScene)) {';
if (!code.includes(importAnchor) || !code.includes(bodyAnchor)) {
  throw new Error('Unbekannte src/server.ts-Struktur. Nichts verändert; siehe README-LIVE.md.');
}
await copyFile(path, path + '.before-live.bak');
code = code.replace(importAnchor, importAnchor + "\nimport { runLive } from './live.js';");
code = code.replace(bodyAnchor, `  // Prefer the open Blender window when its Live bridge is running.\n  // FRAMEPILOT_MODE=headless forces the original, separate Blender process.\n  // FRAMEPILOT_MODE=live requires a connection (never silently falls back).\n  if (process.env.FRAMEPILOT_MODE !== 'headless' &&\n      (process.env.FRAMEPILOT_MODE === 'live' || existsSync(resolve(runtimeDir, 'framepilot-live.json')))) {\n    return runLive(action, { ...payload, previewPath });\n  }\n` + bodyAnchor);
await writeFile(path, code);
console.log('FramePilot Live aktiviert. Backup: src/server.ts.before-live.bak');
console.log('Deine bestehenden Tools und deine Codex-/LangGraph-Einstellungen bleiben erhalten.');
