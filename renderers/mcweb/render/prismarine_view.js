#!/usr/bin/env node
/*
 * prismarine_view.js — render an exported Anvil world through the ACTUAL
 * prismarine-viewer browser mesher (the same renderer minecraft-web-client is
 * built on) and screenshot it in headless Chromium. This is the real-renderer
 * confirmation, not the flat-shaded model bake in bake.js/shot.mjs.
 *
 * It serves prismarine-viewer's prebuilt browser bundle via its `standalone`
 * server (streaming our chunks over socket.io) and drives an OrbitControls
 * camera with the mouse. We require 'prismarine-viewer/lib/standalone' directly
 * so we don't pull in the node-side viewer, which needs native `canvas`/`gl`
 * (those don't build in a headless sandbox — the browser bundle uses WebGL via
 * Chromium/swiftshader instead).
 *
 * The world version must be one the bundle ships (…/public/blocksStates/*.json;
 * 1.20.1 works) — export with `export_anvil.js --version 1.20.1`.
 *
 * Usage:
 *   node prismarine_view.js <world-dir> --center X,Y,Z [--view 4] [--zoom 3]
 *        [--orbit DX,DY] [--out shot.png] [--version 1.20.1]
 */
'use strict'

const path = require('path')
const { Vec3 } = require('vec3')
const { chromium } = require('playwright-core')

function arg(name, def) {
  const i = process.argv.indexOf(name)
  return i >= 0 ? process.argv[i + 1] : def
}

async function main() {
  const worldDir = path.resolve(process.argv[2])
  const version = arg('--version', '1.20.1')
  const [cx, cy, cz] = arg('--center', '0,8,0').split(',').map(Number)
  const viewDistance = Number(arg('--view', 4))
  const zoom = Number(arg('--zoom', 3))
  const [odx, ody] = arg('--orbit', '0,0').split(',').map(Number)
  const out = path.resolve(arg('--out', 'prismarine_view.png'))
  const port = 3021

  const Anvil = require('prismarine-provider-anvil').Anvil(version)
  const Chunk = require('prismarine-chunk')(version)
  const provider = new Anvil(path.join(worldDir, 'region'))
  const empty = () => new Chunk({ minY: -64, worldHeight: 384 })
  // standalone only needs world.getColumn(cx,cz).toJson(); back it with our save
  const world = { getColumn: async (a, b) => { try { return (await provider.load(a, b)) || empty() } catch { return empty() } } }

  require('prismarine-viewer/lib/standalone')({ version, world, center: new Vec3(cx, cy, cz), viewDistance, port })
  await new Promise((r) => setTimeout(r, 800))

  const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium',
    args: ['--use-gl=angle', '--use-angle=swiftshader', '--no-sandbox', '--ignore-gpu-blocklist'] })
  const p = await browser.newPage({ viewport: { width: 1000, height: 680 } })
  p.on('pageerror', (e) => console.error('[pageerror]', e.message))
  await p.goto(`http://localhost:${port}`)
  await p.waitForTimeout(8000)                 // let the worker mesh the chunks
  const X = 500, Y = 340
  await p.mouse.move(X, Y)
  for (let i = 0; i < zoom; i++) { await p.mouse.wheel(0, -140); await p.waitForTimeout(80) }
  if (odx || ody) { await p.mouse.move(X, Y); await p.mouse.down(); await p.mouse.move(X + odx, Y + ody, { steps: 24 }); await p.mouse.up() }
  await p.waitForTimeout(1500)
  await p.screenshot({ path: out })
  console.error('wrote', out)
  await browser.close()
  process.exit(0)
}

main().catch((e) => { console.error(e.stack || String(e)); process.exit(1) })
