#!/usr/bin/env node
/*
 * export_anvil.js — voxel blocks.csv  ->  Java Edition Anvil world save
 *
 * The BlockCraft renderer (../..//blockcraft) is a cube-only engine, so
 * scripts/export_blockcraft.py has to down-map our palette and fake doors as
 * flat panels. This exporter targets zardoy/minecraft-web-client instead,
 * which renders *real vanilla block models*. So we keep every block-state
 * verbatim — `minecraft:oak_door[facing=...,half=...]`, `*_stairs[...]`,
 * `*_slab[type=...]`, stained glass, connected `iron_bars` — and write a
 * standard Anvil save that the client loads directly (drag-and-drop a zip, or
 * serve the folder). No engine patching, no custom door block.
 *
 * We build the save with the prismarine stack (prismarine-chunk +
 * prismarine-provider-anvil) — the *same* libraries minecraft-web-client uses
 * to load a world — so compatibility is guaranteed by construction.
 *
 * Input  CSV columns: x,y,z,block   (block = full Minecraft block-state string;
 *                                     identical schema to scripts/ifc_to_voxels.py)
 * Output world/  { level.dat, region/r.*.mca }
 *
 * Usage:
 *   node export_anvil.js blocks.csv --out world [--version 1.20.4]
 *        [--base-y 4] [--no-floor] [--name "UNBC"]
 */
'use strict'

const fs = require('fs')
const path = require('path')
const zlib = require('zlib')

function parseArgs(argv) {
  const a = { version: '1.20.4', out: 'world', baseY: 4, floor: true, name: 'bimmer' }
  const rest = []
  for (let i = 0; i < argv.length; i++) {
    const t = argv[i]
    if (t === '--out') a.out = argv[++i]
    else if (t === '--version') a.version = argv[++i]
    else if (t === '--base-y') a.baseY = parseInt(argv[++i], 10)
    else if (t === '--name') a.name = argv[++i]
    else if (t === '--no-floor') a.floor = false
    else if (t.startsWith('--')) throw new Error('unknown flag ' + t)
    else rest.push(t)
  }
  if (!rest.length) throw new Error('usage: export_anvil.js <blocks.csv> [--out DIR] [--version V]')
  a.input = rest[0]
  return a
}

// "minecraft:oak_door[facing=east,half=lower]" -> { name, props }
function parseState(str) {
  const m = str.trim().match(/^([^\[]+)(?:\[(.*)\])?$/)
  if (!m) throw new Error('bad block string: ' + str)
  const name = m[1].replace(/^minecraft:/, '')
  const props = {}
  if (m[2]) {
    for (const kv of m[2].split(',')) {
      const eq = kv.indexOf('=')
      if (eq < 0) continue
      const k = kv.slice(0, eq).trim()
      const v = kv.slice(eq + 1).trim()
      props[k] = v === 'true' ? true : v === 'false' ? false : /^-?\d+$/.test(v) ? parseInt(v, 10) : v
    }
  }
  return { name, props }
}

function readCsv(file) {
  const text = fs.readFileSync(file, 'utf8')
  const lines = text.split(/\r?\n/)
  const header = lines[0].split(',').map((s) => s.trim())
  const ix = header.indexOf('x'), iy = header.indexOf('y'), iz = header.indexOf('z'), ib = header.indexOf('block')
  if (ix < 0 || iy < 0 || iz < 0) throw new Error('CSV must have x,y,z[,block] columns')
  const rows = []
  for (let i = 1; i < lines.length; i++) {
    const ln = lines[i]
    if (!ln.trim()) continue
    const c = ln.split(',')
    // block-state strings contain commas, so the CSV writer quotes them:
    // x,y,z,"minecraft:oak_door[facing=east,half=lower,...]" — rejoin + unquote.
    let block = ib >= 0 ? c.slice(ib).join(',').trim() : 'minecraft:stone'
    if (block.startsWith('"') && block.endsWith('"')) block = block.slice(1, -1).replace(/""/g, '"')
    rows.push({ x: parseInt(c[ix], 10), y: parseInt(c[iy], 10), z: parseInt(c[iz], 10), block })
  }
  if (!rows.length) throw new Error('no block rows in ' + file)
  return rows
}

function writeLevelDat(worldDir, opts, registry) {
  const nbt = require('prismarine-nbt')
  const dataVersion = (registry.version && registry.version.dataVersion) || 3700
  const int = (value) => ({ type: 'int', value })
  const byte = (value) => ({ type: 'byte', value })
  const data = {
    type: 'compound',
    name: '',
    value: {
      Data: {
        type: 'compound',
        value: {
          version: int(19133),
          DataVersion: int(dataVersion),
          // minecraft-web-client (and vanilla) read the world version from
          // Data.Version.Name; without it the client falls back to 1.8.8 and
          // mis-parses the (flattened, 1.18+) chunks -> "reading 'Sections'".
          Version: {
            type: 'compound',
            value: {
              Id: int(dataVersion),
              Name: { type: 'string', value: opts.version },
              Snapshot: byte(0),
            },
          },
          LevelName: { type: 'string', value: opts.name },
          GameType: int(1), // creative
          allowCommands: byte(1),
          Difficulty: byte(1),
          SpawnX: int(opts.spawn[0]),
          SpawnY: int(opts.spawn[1]),
          SpawnZ: int(opts.spawn[2]),
          Time: { type: 'long', value: [0, 0] },
          DayTime: { type: 'long', value: [0, 6000] }, // midday
          raining: byte(0),
          thundering: byte(0),
          hardcore: byte(0),
          initialized: byte(1),
          generatorName: { type: 'string', value: 'flat' },
        },
      },
    },
  }
  const buf = nbt.writeUncompressed(data, 'big')
  fs.writeFileSync(path.join(worldDir, 'level.dat'), zlib.gzipSync(buf))
}

async function main() {
  const opts = parseArgs(process.argv.slice(2))
  const registry = require('prismarine-registry')(opts.version)
  const Chunk = require('prismarine-chunk')(opts.version)
  const Block = require('prismarine-block')(opts.version)
  const Anvil = require('prismarine-provider-anvil').Anvil(opts.version)

  // block-string -> Block, seeded from the block's DEFAULT state so unspecified
  // props (waterlogged/open/powered/...) don't get spurious values.
  const blockCache = new Map()
  function makeBlock(str) {
    let b = blockCache.get(str)
    if (b) return b
    const { name, props } = parseState(str)
    const bd = registry.blocksByName[name]
    if (!bd) return null
    const base = Block.fromStateId(bd.defaultState, 0).getProperties()
    b = Block.fromProperties(bd.id, Object.assign({}, base, props), 0)
    blockCache.set(str, b)
    return b
  }

  const rows = readCsv(opts.input)
  // bounds via a loop — spreading 100k+ rows into Math.min/max overflows the stack
  let minX = Infinity, minY = Infinity, minZ = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity
  for (const r of rows) {
    if (r.x < minX) minX = r.x; if (r.x > maxX) maxX = r.x
    if (r.y < minY) minY = r.y; if (r.y > maxY) maxY = r.y
    if (r.z < minZ) minZ = r.z; if (r.z > maxZ) maxZ = r.z
  }

  // world placement: shift building to non-negative, sit its lowest voxel at baseY
  const shift = (r) => ({ x: r.x - minX, y: r.y - minY + opts.baseY, z: r.z - minZ, block: r.block })

  const chunks = new Map() // "cx,cz" -> Chunk
  function chunkAt(cx, cz) {
    const key = cx + ',' + cz
    let c = chunks.get(key)
    if (!c) { c = new Chunk({ minY: -64, worldHeight: 384 }); chunks.set(key, c) }
    return c
  }
  function setWorld(x, y, z, block) {
    const cx = x >> 4, cz = z >> 4
    chunkAt(cx, cz).setBlock({ x: x & 15, y, z: z & 15 }, block)
  }

  // optional flat grass ground one below the building, spanning the footprint (+margin)
  let floorCount = 0
  if (opts.floor) {
    const groundY = opts.baseY - 1
    const grass = makeBlock('minecraft:grass_block')
    const dirt = makeBlock('minecraft:dirt')
    const margin = 4
    for (let x = -margin; x <= (maxX - minX) + margin; x++) {
      for (let z = -margin; z <= (maxZ - minZ) + margin; z++) {
        setWorld(x, groundY, z, grass)
        setWorld(x, groundY - 1, z, dirt)
        setWorld(x, groundY - 2, z, dirt)
        floorCount++
      }
    }
  }

  let placed = 0, unknown = 0
  const unknownNames = new Set()
  const counts = new Map()
  for (const raw of rows) {
    const r = shift(raw)
    const b = makeBlock(r.block)
    if (!b) { unknown++; unknownNames.add(parseState(r.block).name); continue }
    setWorld(r.x, r.y, r.z, b)
    placed++
    const base = r.block.split('[')[0]
    counts.set(base, (counts.get(base) || 0) + 1)
  }

  // centre spawn just above the building's roof (clamped under the world height)
  const buildTop = opts.baseY + (maxY - minY)
  opts.spawn = [Math.floor((maxX - minX) / 2), Math.min(buildTop + 6, 315), Math.floor((maxZ - minZ) / 2)]

  const worldDir = path.resolve(opts.out)
  fs.mkdirSync(path.join(worldDir, 'region'), { recursive: true })
  const anvil = new Anvil(path.join(worldDir, 'region'))
  for (const [key, c] of chunks) {
    const [cx, cz] = key.split(',').map(Number)
    await anvil.save(cx, cz, c)
  }
  if (anvil.close) await anvil.close()
  writeLevelDat(worldDir, opts, registry)

  const summary = {
    input: opts.input,
    out: worldDir,
    version: opts.version,
    blocks_placed: placed,
    floor_blocks: floorCount,
    chunks: chunks.size,
    unknown_blocks: unknown,
    unknown_names: [...unknownNames].sort(),
    spawn: opts.spawn,
    counts: Object.fromEntries([...counts.entries()].sort((a, b) => b[1] - a[1])),
  }
  fs.writeFileSync(path.join(worldDir, 'export_summary.json'), JSON.stringify(summary, null, 2))
  console.log(JSON.stringify(summary, null, 2))
}

main().catch((e) => { console.error(e.stack || String(e)); process.exit(1) })
