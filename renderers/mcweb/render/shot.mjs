// shot.mjs — render index.html (real vanilla block models baked by bake.js) in
// headless Chromium/WebGL and screenshot it to render.png.
//   node bake.js && node shot.mjs
// Chromium path defaults to the PLAYWRIGHT bundle; override with $PW_CHROMIUM.
import { chromium } from 'playwright-core'
import http from 'http'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import { createRequire } from 'module'

const dir = path.dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)
// three ships only as ESM chunks; copy them next to index.html so the import
// map resolves over http (these copies are git-ignored). Resolve three from
// node_modules wherever npm put it (this dir or renderers/mcweb).
const threeBuild = path.dirname(require.resolve('three')) // .../three/build
// three.module.min.js is self-contained in r160; newer builds split out
// three.core.min.js — copy whichever exist so the import map resolves.
for (const f of ['three.module.min.js', 'three.core.min.js']) {
  const src = path.join(threeBuild, f)
  if (fs.existsSync(src)) fs.copyFileSync(src, path.join(dir, f))
}

const types = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json' }
const srv = http.createServer((req, res) => {
  let f = decodeURIComponent(req.url.split('?')[0]); if (f === '/') f = '/index.html'
  try {
    const buf = fs.readFileSync(path.join(dir, f))
    res.writeHead(200, { 'content-type': types[path.extname(f)] || 'application/octet-stream' }); res.end(buf)
  } catch { res.writeHead(404); res.end('nf') }
})
await new Promise((r) => srv.listen(8799, r))

const exe = process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium'
const b = await chromium.launch({ executablePath: exe,
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--no-sandbox', '--ignore-gpu-blocklist'] })
const p = await b.newPage({ viewport: { width: 900, height: 520 } })
p.on('pageerror', (e) => console.error('[pageerror]', e.message))
await p.goto('http://localhost:8799/index.html')
await p.waitForFunction('window.__ready===true', { timeout: 20000 })
await p.waitForTimeout(400)
await p.screenshot({ path: path.join(dir, 'render.png') })
console.error('screenshot -> render.png')
await b.close(); srv.close()
