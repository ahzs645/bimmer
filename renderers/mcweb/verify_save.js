#!/usr/bin/env node
/*
 * verify_save.js — load an exported Anvil save with prismarine-provider-anvil
 * (the SAME loader minecraft-web-client uses) and confirm block-states survived
 * the write. It SCANS the saved chunks (works on any world — the demo fixture or
 * a real building) and reports capabilities rather than fixed coordinates.
 *
 * Required: the world loads, and functional doors round-trip (both halves, each
 * with a facing — proving doors are oriented, not guessed). Everything else is
 * reported as present/absent, since which shapes appear depends on the model and
 * on what the voxelizer currently emits (see RENDERERS.md → pipeline output).
 *
 * Usage:  node verify_save.js <world-dir> [--version 1.20.4]
 */
'use strict'

const fs = require('fs')
const path = require('path')

async function main() {
  const args = process.argv.slice(2)
  let version = '1.20.4'
  const rest = []
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--version') version = args[++i]
    else rest.push(args[i])
  }
  const worldDir = path.resolve(rest[0] || 'world')
  const regionDir = path.join(worldDir, 'region')
  const Anvil = require('prismarine-provider-anvil').Anvil(version)
  const anvil = new Anvil(regionDir)

  // chunk coords to scan, derived from region file names r.<rx>.<rz>.mca
  const chunkCoords = []
  for (const f of fs.readdirSync(regionDir)) {
    const m = f.match(/^r\.(-?\d+)\.(-?\d+)\.mca$/)
    if (!m) continue
    const rx = +m[1], rz = +m[2]
    for (let dx = 0; dx < 32; dx++) for (let dz = 0; dz < 32; dz++) chunkCoords.push([rx * 32 + dx, rz * 32 + dz])
  }

  // name -> Set of interesting state signatures seen; plus overall type counts
  const seen = new Map()
  const counts = new Map()
  const noteProps = { oak_door: ['half', 'facing'], stone_brick_stairs: ['facing', 'half', 'shape'],
    smooth_stone_slab: ['type'], oak_fence: ['north', 'east', 'south', 'west'] }
  let scannedChunks = 0
  let voidColumns = 0, signEntities = 0, signsWithText = 0
  for (const [cx, cz] of chunkCoords) {
    let chunk
    try { chunk = await anvil.load(cx, cz) } catch { chunk = null }
    if (!chunk) continue
    scannedChunks++
    // a saved chunk is never regenerated, so an empty bottom is a hole to the void
    for (let x = 0; x < 16; x++) for (let z = 0; z < 16; z++) {
      const b = chunk.getBlock({ x, y: -64, z })
      if (!b || b.name === 'air') voidColumns++
    }
    for (let x = 0; x < 16; x++) for (let z = 0; z < 16; z++) for (let y = -8; y < 60; y++) {
      const b = chunk.getBlock({ x, y, z })
      if (!b || b.name === 'air' || b.name === 'cave_air') continue
      counts.set(b.name, (counts.get(b.name) || 0) + 1)
      if (!seen.has(b.name)) seen.set(b.name, new Set())
      const keys = noteProps[b.name]
      if (keys && keys.length) seen.get(b.name).add(keys.map((k) => `${k}=${b.getProperties()[k]}`).join(';'))
    }
  }

  const palette = [...counts.entries()].sort((a, b) => b[1] - a[1])
  console.log(`loaded ${scannedChunks} chunks; palette: ${palette.map(([n, c]) => `${n}×${c}`).join(', ')}\n`)

  let fail = 0
  // --- REQUIRED: functional, oriented doors round-trip ---
  const doorSigs = seen.get('oak_door') || new Set()
  const halves = new Set([...doorSigs].map((s) => (s.match(/half=(\w+)/) || [])[1]))
  const facings = new Set([...doorSigs].map((s) => (s.match(/facing=(\w+)/) || [])[1]).filter(Boolean))
  const doorsOk = counts.get('oak_door') > 0 && halves.has('lower') && halves.has('upper') && facings.size > 0
  console.log(`${doorsOk ? 'PASS' : 'FAIL'}  functional doors round-trip  (halves=${[...halves].join('/')}, facings=${[...facings].join('/') || 'none'})`)
  if (!doorsOk) fail++

  // --- REPORTED capabilities (present-and-valid / present-but-broken / absent) ---
  const caps = [
    ['stone_brick_stairs', 'shape', 'real stair block-states'],
    ['smooth_stone_slab', 'type', 'real slabs'],
    ['oak_fence', 'north', 'fence railings (connection states)'],
    ['light_blue_stained_glass', null, 'stained-glass glazing'],
  ]
  for (const [name, prop, label] of caps) {
    const n = counts.get(name) || 0
    if (!n) { console.log(`  --   ${label}: absent in this model`); continue }
    if (prop) {
      const ok = [...(seen.get(name) || [])].some((s) => new RegExp(`${prop}=\\w`).test(s))
      console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${n} blocks${ok ? '' : ' — state MISSING'}`)
      if (!ok) fail++
    } else {
      console.log(`PASS  ${label}: ${n} blocks`)
    }
  }

  // --- signs: every sign block carries its text (block + block entity) ---
  // Read from the raw region NBT, not the loaded chunks: prismarine-chunk's
  // loadBlockEntities keys entities by x>>4 (the CHUNK index) instead of x&15,
  // so every sign in a chunk at one height collapses onto one key on load.
  {
    const zlib = require('zlib')
    const nbt = require('prismarine-nbt')
    for (const f of fs.readdirSync(regionDir)) {
      if (!/\.mca$/.test(f)) continue
      const buf = fs.readFileSync(path.join(regionDir, f))
      for (let i = 0; i < 1024; i++) {
        const off = buf.readUInt32BE(i * 4) >> 8
        if (!off) continue
        const len = buf.readUInt32BE(off * 4096)
        const raw = buf.slice(off * 4096 + 5, off * 4096 + 4 + len)
        const tag = nbt.simplify(nbt.parseUncompressed(buf[off * 4096 + 4] === 1 ? zlib.gunzipSync(raw) : zlib.inflateSync(raw), 'big'))
        for (const be of tag.block_entities || []) {
          if (be.id !== 'minecraft:sign') continue
          signEntities++
          if (be.front_text && be.front_text.messages.some((m) => JSON.parse(m).text)) signsWithText++
        }
      }
    }
  }
  const signBlocks = [...counts.entries()].filter(([n]) => /_sign$/.test(n)).reduce((a, [, c]) => a + c, 0)
  if (!signBlocks && !signEntities) console.log('  --   room signs: absent in this world')
  else {
    const ok = signEntities === signBlocks && signsWithText === signEntities
    console.log(`${ok ? 'PASS' : 'FAIL'}  room signs: ${signBlocks} blocks, ${signEntities} entities, ${signsWithText} with text`)
    if (!ok) fail++
  }

  // --- playable in vanilla: ground under every saved column, world-gen + rules ---
  console.log(`${voidColumns ? 'FAIL' : 'PASS'}  ground under every saved column (${voidColumns} open to the void)`)
  if (voidColumns) fail++
  try {
    const nbt = require('prismarine-nbt')
    const { parsed } = await nbt.parse(fs.readFileSync(path.join(worldDir, 'level.dat')))
    const D = nbt.simplify(parsed).Data
    const gen = D.WorldGenSettings && D.WorldGenSettings.dimensions['minecraft:overworld'].generator
    const ok = gen && gen.type === 'minecraft:flat' && D.GameRules && D.GameRules.doMobSpawning === 'false'
    console.log(`${ok ? 'PASS' : 'FAIL'}  level.dat world-gen + gamerules (generator=${gen && gen.type}, spawn=${D.SpawnX},${D.SpawnY},${D.SpawnZ})`)
    if (!ok) fail++
  } catch (e) { console.log('FAIL  level.dat unreadable: ' + e.message); fail++ }

  console.log(fail ? `\n${fail} check(s) failed` : '\nall required checks passed')
  if (fail) process.exit(1)
}

main().catch((e) => { console.error(e.stack || String(e)); process.exit(1) })
