import { cp, mkdir, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const output = join(root, 'dist');
await rm(output, { recursive: true, force: true });
await mkdir(join(output, 'vendor'), { recursive: true });
for (const file of ['index.html', 'app.js', 'callback.html', 'callback.js', 'styles.css', 'provider.css', 'duplicate-cleanup.css', 'favicon.png']) {
  await cp(join(root, file), join(output, file));
}
await cp(join(root, 'node_modules/@trimjs/web-app/dist/index.js'), join(output, 'vendor/trim-web-app.js'));
