import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { existsSync, mkdirSync, rmSync } from 'node:fs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const blender = process.env.BLENDER_PATH || (process.platform === 'darwin'
  ? '/Applications/Blender.app/Contents/MacOS/Blender' : 'blender');
const scene = join(root, 'scene.blend');
const session = join(root, 'runtime', 'session.blend');
if (existsSync(scene)) {
  console.error('scene.blend existiert schon. Kein Überschreiben. Nutze die bestehende Szene oder benenne sie um.');
  process.exit(1);
}
mkdirSync(join(root, 'runtime'), { recursive: true });
try {
  execFileSync(blender, ['--background', '--factory-startup', '--python-exit-code', '1', '--python', join(root, 'scripts', 'create_demo.py'), '--', scene], {
    cwd: root, stdio: 'inherit', timeout: 120_000,
  });
  rmSync(session, { force: true });
  console.log(`Demo-Szene erstellt: ${scene}`);
} catch (e) {
  console.error(`Blender konnte nicht gestartet werden. BLENDER_PATH prüfen: ${blender}`);
  process.exit(1);
}
