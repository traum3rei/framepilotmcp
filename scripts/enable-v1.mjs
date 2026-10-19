/** Non-destructive, idempotent upgrade: register generic tools without overwriting user tools. */
import { readFile, writeFile, copyFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const path = resolve(root, 'src/server.ts');
const code = await readFile(path, 'utf8');
const marker = 'registerV1Tools(server, (action, payload) => serialized(() => runLive(action, payload)));';
const line = "import { registerV1Tools } from './v1_tools.js';";
if (code.includes(marker) && code.includes(line)) {
  console.log('FramePilot v1 tools already registered. No changes.');
  process.exit(0);
}
if (code.includes(marker) || code.includes(line)) throw new Error('Partial FramePilot v1 registration detected. No changes made.');
if (!code.includes("import { runLive } from './live.js';")) {
  throw new Error('FramePilot Live is required first. Run node scripts/enable-live.mjs before v1. No changes made.');
}
const anchor = "import { McpServer } from '@modelcontextprotocol/server';";
const returnMarker = '  return server;';
if (code.split(anchor).length !== 2 || code.split(returnMarker).length !== 2 || !code.includes('function createServer()')) {
  throw new Error('Unrecognized server.ts structure; no changes made. See README-V1.md.');
}
await copyFile(path, path + '.before-v1.bak');
let updated = code.replace(anchor, anchor + '\n' + line);
updated = updated.replace(returnMarker, `  ${marker}\n${returnMarker}`);
await writeFile(path, updated);
console.log('Registered generic FramePilot v1 MCP tools; original preserved as src/server.ts.before-v1.bak.');
