const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
// A small code-drawn mark; no external art or character assets in the shell.
const size = 256, pixels = Buffer.alloc(size * size * 4);
for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
  const dx = Math.abs(x / 8 - 15.5), dy = Math.abs(y / 8 - 15.5);
  const ink = dx + dy < 13 && (dx + dy > 8 || dx < 2);
  const offset = ((size - 1 - y) * size + x) * 4;
  pixels.set(ink ? [197, 213, 131, 255] : [43, 32, 24, 255], offset);
}
const header = Buffer.alloc(22 + 40), mask = Buffer.alloc(size * size / 8);
header.writeUInt16LE(1, 2); header.writeUInt16LE(1, 4);
header[6] = header[7] = size; header.writeUInt16LE(1, 10); header.writeUInt16LE(32, 12);
header.writeUInt32LE(40 + pixels.length + mask.length, 14); header.writeUInt32LE(22, 18);
header.writeUInt32LE(40, 22); header.writeInt32LE(size, 26); header.writeInt32LE(size * 2, 30);
header.writeUInt16LE(1, 34); header.writeUInt16LE(32, 36); header.writeUInt32LE(pixels.length, 42);
fs.writeFileSync(path.join(root, 'ui/icon.ico'), Buffer.concat([header, pixels, mask]));
const licenses = path.join(root, 'licenses');
fs.mkdirSync(licenses, {recursive: true});
// The native build receives these reviewed text files along with the source inputs.
if (fs.existsSync(path.join(root, '../../LICENSE'))) {
  for (const name of ['LICENSE', 'COPYRIGHT']) fs.copyFileSync(path.join(root, '../..', name), path.join(licenses, name));
  for (const name of ['Open-LLM-VTuber-LICENSE.txt', 'Open-LLM-VTuber-Web-LICENSE.txt'])
    fs.copyFileSync(path.join(root, '../open_llm_vtuber', name), path.join(licenses, name));
}
for (const name of ['LICENSE', 'COPYRIGHT', 'Open-LLM-VTuber-LICENSE.txt', 'Open-LLM-VTuber-Web-LICENSE.txt'])
  if (!fs.existsSync(path.join(licenses, name))) throw new Error('Missing license material: ' + name);
