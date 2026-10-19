/** Optional macOS Blender add-on installer; does not modify an open Blender session. */
import { copyFile, mkdir, access } from 'node:fs/promises';
import { homedir, platform } from 'node:os';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const version = process.argv[2] || '5.2';
if (!/^\d+\.\d+$/.test(version)) throw new Error('Usage: node scripts/install-v1-addon.mjs 5.2');
if (platform() !== 'darwin') throw new Error('This convenience installer is for macOS. Install addon via Blender Preferences on other systems.');
const root=resolve(dirname(fileURLToPath(import.meta.url)), '..');
const source=join(root,'addons','framepilot_live.py');
const addons=join(homedir(),'Library','Application Support','Blender',version,'scripts','addons');
const destination=join(addons,'framepilot_live.py');
await mkdir(addons,{recursive:true});
try {
  await access(destination);
  const backup=destination+'.before-v1.bak';
  await copyFile(destination,backup);
  console.log('Backed up existing Blender addon:',backup);
} catch (e) {
  if (e.code !== 'ENOENT') throw e;
}
await copyFile(source,destination);
console.log('Installed FramePilot v1 addon:',destination);
console.log('RESTART Blender before starting the Live Bridge again.');
