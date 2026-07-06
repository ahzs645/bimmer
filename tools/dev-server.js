#!/usr/bin/env node
// dev-server.js — one command to run both renderers locally, no deps.
//
//   npm run dev   (or: pnpm dev / node tools/dev-server.js)
//
//   :8080  chooser page (pick a renderer)
//   :8091  minecraft-web-client — proxied from mcraft.fun through curl
//          (works behind egress proxies / in sandboxes), serving the
//          committed renderers/mcweb/unbc_world.zip and booting straight
//          into it, exactly like the deployed GitHub Page
//   :8092  BlockCraft — static build from blockcraft/client/dist
//          (build once: scripts/build_blockcraft_static.sh)
//
// Ports override: PORT=9000 node tools/dev-server.js  -> 9000/9001/9002
const http = require('http')
const { execFile } = require('child_process')
const fs = require('fs')
const path = require('path')

const ROOT = path.join(__dirname, '..')
const BASE = parseInt(process.env.PORT || '8080', 10)
const CACHE = path.join(ROOT, '.cache', 'mcraft')
fs.mkdirSync(CACHE, { recursive: true })
const UPSTREAM = 'https://mcraft.fun'
const WORLD = path.join(ROOT, 'renderers', 'mcweb', 'unbc_world.zip')
const BC_DIST = path.join(ROOT, 'blockcraft', 'client', 'dist')

const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.webp': 'image/webp', '.wasm': 'application/wasm', '.zip': 'application/zip',
  '.map': 'application/json', '.woff2': 'font/woff2', '.mp3': 'audio/mpeg',
  '.webm': 'video/webm', '.svg': 'image/svg+xml', '.ico': 'image/x-icon' }

function send (res, buf, ct, code = 200) {
  res.writeHead(code, { 'content-type': ct || 'application/octet-stream',
    'cross-origin-embedder-policy': 'require-corp',
    'cross-origin-opener-policy': 'same-origin',
    'access-control-allow-origin': '*' })
  res.end(buf)
}

// ---- :8080 chooser -------------------------------------------------------
const chooser = `<!doctype html><meta charset="utf-8">
<title>bimmer — pick a renderer</title>
<style>body{font:16px/1.5 system-ui;margin:8vh auto;max-width:640px;padding:0 1rem}
a.card{display:block;border:1px solid #8884;border-radius:12px;padding:1rem 1.2rem;
margin:1rem 0;text-decoration:none;color:inherit}a.card:hover{border-color:#888}
h1{font-size:1.4rem}small{opacity:.7}</style>
<h1>UNBC campus — IFC &rarr; Minecraft</h1>
<a class="card" href="http://localhost:${BASE + 11}/">
  <b>minecraft-web-client</b> — the real thing<br>
  <small>vanilla block models: openable doors, oriented stairs, connected
  fences. Boots straight into the building. (proxied from mcraft.fun,
  world served locally)</small></a>
<a class="card" href="http://localhost:${BASE + 12}/">
  <b>BlockCraft</b> — lightweight cube engine<br>
  <small>fully serverless build; needs
  <code>scripts/build_blockcraft_static.sh</code> run once</small></a>
<p><small>Deployed equivalents: <code>/</code> and <code>/blockcraft/</code>
on the GitHub Pages site. See RENDERERS.md.</small></p>`

http.createServer((req, res) => send(res, chooser, 'text/html'))
  .listen(BASE, () => console.log(`chooser            http://localhost:${BASE}`))

// ---- :8091 minecraft-web-client relay ------------------------------------
http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0])
  if (p === '/') p = '/index.html'
  try {
    if (p === '/world.zip') return send(res, fs.readFileSync(WORLD), 'application/zip')
    const cacheFile = path.join(CACHE, p.replace(/[^a-zA-Z0-9._-]/g, '_'))
    const finish = (buf) => {
      if (p === '/index.html') {
        let html = buf.toString().replace('<head>',
          '<head><script>if(!location.search)location.replace(location.pathname+' +
          '"?map=world.zip&setting=renderDebug:%22none%22"+location.hash)</script>')
        return send(res, html, 'text/html')
      }
      send(res, buf, types[path.extname(p)])
    }
    if (fs.existsSync(cacheFile) && fs.statSync(cacheFile).size > 0) {
      return finish(fs.readFileSync(cacheFile))
    }
    execFile('curl', ['-sf', '--max-time', '120', UPSTREAM + p],
      { maxBuffer: 1 << 28, encoding: 'buffer' }, (err, stdout) => {
        if (err) return send(res, 'not found', 'text/plain', 404)
        fs.writeFileSync(cacheFile, stdout)
        finish(stdout)
      })
  } catch (e) { send(res, 'err: ' + e.message, 'text/plain', 500) }
}).listen(BASE + 11, () => console.log(`minecraft-web-client http://localhost:${BASE + 11}`))

// ---- :8092 BlockCraft static ----------------------------------------------
http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0])
  if (p === '/') p = '/index.html'
  const f = path.join(BC_DIST, path.normalize(p).replace(/^([.][.][/\\])+/, ''))
  if (!f.startsWith(BC_DIST)) return send(res, 'nope', 'text/plain', 403)
  fs.readFile(f, (err, buf) => {
    if (err) {
      return send(res, 'BlockCraft is not built yet.\n\nRun once:\n  scripts/build_blockcraft_static.sh\n',
        'text/plain', fs.existsSync(BC_DIST) ? 404 : 503)
    }
    send(res, buf, types[path.extname(f)])
  })
}).listen(BASE + 12, () => console.log(`blockcraft          http://localhost:${BASE + 12}`))
