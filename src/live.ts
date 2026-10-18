/** Tiny authenticated JSON-over-localhost bridge to the currently open Blender window. */
import { createConnection } from 'node:net';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const connectionFile = resolve(root, 'runtime', 'framepilot-live.json');

export async function runLive(action: string, payload: Record<string, unknown>): Promise<unknown> {
  let config: { host: string; port: number; token: string };
  try {
    config = JSON.parse(await readFile(connectionFile, 'utf8'));
  } catch {
    throw new Error('FramePilot Live ist nicht gestartet: Blender > N-Panel > FramePilot > Start Live Bridge.');
  }
  if (config.host !== '127.0.0.1' || !Number.isInteger(config.port) ||
      config.port < 1 || config.port > 65535 || typeof config.token !== 'string' || config.token.length < 32) {
    throw new Error('Ungültige FramePilot-Live-Konfiguration. Bridge in Blender neu starten.');
  }
  const timeoutMs = action === 'render' ? 250_000 : 45_000;
  return new Promise<unknown>((resolveResult, rejectResult) => {
    const socket = createConnection({ host: config.host, port: config.port });
    let complete = false;
    let buffer = '';
    const fail = (error: Error) => {
      if (complete) return;
      complete = true;
      socket.destroy();
      rejectResult(error);
    };
    socket.setTimeout(timeoutMs, () => fail(new Error('FramePilot Live antwortet nicht. Blender ggf. beschäftigt.')));
    socket.once('connect', () => {
      socket.write(JSON.stringify({ token: config.token, action, payload }) + '\n');
    });
    socket.on('data', (chunk: Buffer) => {
      if (complete) return;
      buffer += chunk.toString('utf8');
      if (buffer.length > 1_000_000) {
        fail(new Error('FramePilot-Live-Antwort ist zu groß.'));
        return;
      }
      const newline = buffer.indexOf('\n');
      if (newline < 0) return;
      try {
        const message: { ok?: boolean; result?: unknown; error?: string } = JSON.parse(buffer.slice(0, newline));
        if (!message.ok) throw new Error(message.error || 'Unbekannter Blender-Fehler');
        complete = true;
        socket.end();
        resolveResult(message.result);
      } catch (e) {
        fail(e instanceof Error ? e : new Error(String(e)));
      }
    });
    socket.once('error', (error) => fail(new Error(`Keine Live-Verbindung zu Blender: ${error.message}`)));
    socket.once('close', () => { if (!complete) fail(new Error('Blender hat die Live-Verbindung geschlossen.')); });
  });
}
