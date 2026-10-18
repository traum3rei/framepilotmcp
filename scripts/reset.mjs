import { rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
const root = join(dirname(fileURLToPath(import.meta.url)), '..');
rmSync(join(root, 'runtime', 'session.blend'), { force: true });
rmSync(join(root, 'runtime', 'preview.png'), { force: true });
console.log('Working copy und Preview zurückgesetzt. Original-Szene bleibt unverändert.');
