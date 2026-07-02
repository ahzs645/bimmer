#!/usr/bin/env node
// door_diag.js — scan an exported Anvil world for door placement pathologies:
// free-standing doors (no wall beside them), doors with holes around them, etc.
'use strict'
const fs = require('fs')
const path = require('path')

async function main() {
  const worldDir = path.resolve(process.argv[2])
  const version = '1.20.1'
  const Anvil = require('prismarine-provider-anvil').Anvil(version)
  const anvil = new Anvil(path.join(worldDir, 'region'))
  const regionDir = path.join(worldDir, 'region')
  const chunks = new Map()
  const coords = []
  for (const f of fs.readdirSync(regionDir)) {
    const m = f.match(/^r\.(-?\d+)\.(-?\d+)\.mca$/)
    if (!m) continue
    for (let dx = 0; dx < 32; dx++) for (let dz = 0; dz < 32; dz++)
      coords.push([+m[1] * 32 + dx, +m[2] * 32 + dz])
  }
  for (const [cx, cz] of coords) {
    try { const c = await anvil.load(cx, cz); if (c) chunks.set(cx + ',' + cz, c) } catch {}
  }
  function blockAt(x, y, z) {
    const c = chunks.get((x >> 4) + ',' + (z >> 4))
    if (!c) return null
    return c.getBlock({ x: x & 15, y, z: z & 15 })
  }
  const doors = []
  for (const [key, c] of chunks) {
    const [cx, cz] = key.split(',').map(Number)
    for (let x = 0; x < 16; x++) for (let z = 0; z < 16; z++) for (let y = 0; y < 30; y++) {
      const b = c.getBlock({ x, y, z })
      if (b && b.name === 'oak_door' && b.getProperties().half === 'lower')
        doors.push({ x: cx * 16 + x, y, z: cz * 16 + z, facing: b.getProperties().facing })
    }
  }
  const solid = (b) => b && b.name !== 'air' && b.name !== 'cave_air' && b.name !== 'oak_door'
  let freeStanding = 0, oneSided = 0, walled = 0
  const freeList = [], oneList = []
  for (const d of doors) {
    // run axis (where the wall should continue): east/west doors run along z, north/south along x
    const rd = (d.facing === 'east' || d.facing === 'west') ? [0, 1] : [1, 0]
    let sides = 0
    for (const s of [-1, 1]) {
      // wall material beside the door at lower or upper height
      if (solid(blockAt(d.x + rd[0] * s, d.y, d.z + rd[1] * s)) ||
          solid(blockAt(d.x + rd[0] * s, d.y + 1, d.z + rd[1] * s))) sides++
    }
    if (sides === 0) { freeStanding++; if (freeList.length < 15) freeList.push(d) }
    else if (sides === 1) { oneSided++; if (oneList.length < 8) oneList.push(d) }
    else walled++
  }
  console.log(`lower door halves: ${doors.length}`)
  console.log(`walled both sides: ${walled}, one side only: ${oneSided}, FREE-STANDING: ${freeStanding}`)
  console.log('free-standing samples:', JSON.stringify(freeList))
  console.log('one-sided samples:', JSON.stringify(oneList))
  // neighborhood dump helper for the first few free doors
  for (const d of freeList.slice(0, 4)) {
    console.log(`--- around (${d.x},${d.y},${d.z}) facing=${d.facing}`)
    for (let y = d.y + 2; y >= d.y - 2; y--) {
      let rows = `y=${y}: `
      for (let z = d.z - 3; z <= d.z + 3; z++) {
        let row = ''
        for (let x = d.x - 3; x <= d.x + 3; x++) {
          const b = blockAt(x, y, z)
          row += !b || b.name === 'air' ? '.' :
            b.name === 'oak_door' ? 'D' : b.name === 'white_concrete' ? 'W' :
            b.name === 'smooth_stone' ? 'F' : b.name.includes('glass') ? 'G' :
            b.name === 'gray_concrete' ? 'm' : b.name === 'oak_fence' ? 'f' :
            b.name.includes('stone_brick') ? 's' : b.name === 'grass_block' || b.name === 'dirt' ? 'g' : '?'
        }
        rows += row + ' | '
      }
      console.log(rows)
    }
  }
}
main().catch((e) => { console.error(e.stack); process.exit(1) })
