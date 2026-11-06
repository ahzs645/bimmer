#!/usr/bin/env node
/*
 * export_anvil.js — voxel blocks.csv  ->  Java Edition Anvil world save
 *
 * The BlockCraft renderer (../..//blockcraft) is a cube-only engine, so
 * scripts/export_blockcraft.py has to down-map our palette and fake doors as
 * flat panels. This exporter targets zardoy/minecraft-web-client instead,
 * which renders *real vanilla block models*. So we keep every block-state
 * verbatim — `minecraft:oak_door[facing=...,half=...]`, `*_stairs[...]`,
 * `*_slab[type=...]`, stained glass, connected `oak_fence` — and write a
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
 *        [--base-y 4] [--no-floor] [--name "UNBC"] [--signs signs.json]
 *        [--spawn X,Y,Z[,YAW]] [--gamemode creative|adventure|survival]
 *
 * Playable in vanilla Java Edition, not only in the web client:
 *   - level.dat carries WorldGenSettings with a FLAT generator whose layers
 *     match the ground written here, so terrain past the saved chunks
 *     continues at the same height instead of whatever default generation
 *     a missing setting falls back to.
 *   - the ground fills EVERY column of every saved chunk (bedrock .. grass).
 *     Vanilla never regenerates a chunk saved with Status=full, so a column
 *     left empty in a saved chunk is a hole to the void, forever.
 *   - gamerules for a static map: no mobs, no daylight/weather cycle, no
 *     fire spread, no griefing, keepInventory, spawnRadius 0.
 *   - spawn on the ground at the widest exterior door that has terrain in
 *     front of it, facing the door (override with --spawn, in blocks.csv
 *     coordinates). The old default dropped players onto the roof.
 *   - --signs places waxed signs (block + block entity) from a JSON list
 *     [{x,y,z,block,lines:[...]}] in blocks.csv coordinates; see
 *     scripts/room_signs.py.
 */
'use strict'

const fs = require('fs')
const path = require('path')
const zlib = require('zlib')

function parseArgs(argv) {
  const a = { version: '1.20.4', out: 'world', baseY: 4, floor: true, name: 'bimmer',
    signs: null, spawn: null, gamemode: 'creative' }
  const rest = []
  for (let i = 0; i < argv.length; i++) {
    const t = argv[i]
    if (t === '--out') a.out = argv[++i]
    else if (t === '--version') a.version = argv[++i]
    else if (t === '--base-y') a.baseY = parseInt(argv[++i], 10)
    else if (t === '--name') a.name = argv[++i]
    else if (t === '--no-floor') a.floor = false
    else if (t === '--signs') a.signs = argv[++i]
    else if (t === '--spawn') a.spawn = argv[++i].split(',').map(Number)
    else if (t === '--gamemode') a.gamemode = argv[++i]
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

const GAMEMODES = { survival: 0, creative: 1, adventure: 2 }

// A static architectural map, not a survival world: nothing spawns, burns,
// grows dark, or rains into the atrium.
const GAMERULES = {
  doMobSpawning: 'false', doDaylightCycle: 'false', doWeatherCycle: 'false',
  doFireTick: 'false', mobGriefing: 'false', keepInventory: 'true',
  doInsomnia: 'false', doPatrolSpawning: 'false', doTraderSpawning: 'false',
  doWardenSpawning: 'false', spawnRadius: '0', announceAdvancements: 'false',
}

// The ground written under every saved column, bottom up. The same list is
// the flat generator's layer list in level.dat, so generated chunks beyond
// the saved ones meet it at the same height.
function groundLayers(groundY, minY = -64) {
  const dirt = 3
  return [
    { block: 'minecraft:bedrock', height: 1 },
    { block: 'minecraft:stone', height: groundY - dirt - minY - 1 },
    { block: 'minecraft:dirt', height: dirt },
    { block: 'minecraft:grass_block', height: 1 },
  ].filter((l) => l.height > 0)
}

function writeLevelDat(worldDir, opts, registry) {
  const nbt = require('prismarine-nbt')
  const dataVersion = (registry.version && registry.version.dataVersion) || 3700
  const int = (value) => ({ type: 'int', value })
  const byte = (value) => ({ type: 'byte', value })
  const str = (value) => ({ type: 'string', value })
  const comp = (value) => ({ type: 'compound', value })
  const list = (type, value) => ({ type: 'list', value: { type, value } })
  const noise = (settings, biomeSource) => comp({
    type: str('minecraft:noise'), settings: str(settings), biome_source: comp(biomeSource) })
  const worldGen = comp({
    bonus_chest: byte(0),
    generate_features: byte(0),
    seed: { type: 'long', value: [0, 0] },
    dimensions: comp({
      'minecraft:overworld': comp({
        type: str('minecraft:overworld'),
        generator: comp({
          type: str('minecraft:flat'),
          settings: comp({
            biome: str('minecraft:plains'),
            features: byte(0),
            lakes: byte(0),
            structure_overrides: list('end', []),
            layers: list('compound', opts.layers.map((l) => ({
              block: str(l.block), height: int(l.height) }))),
          }),
        }),
      }),
      'minecraft:the_nether': comp({
        type: str('minecraft:the_nether'),
        generator: noise('minecraft:nether', {
          type: str('minecraft:multi_noise'), preset: str('minecraft:nether') }),
      }),
      'minecraft:the_end': comp({
        type: str('minecraft:the_end'),
        generator: noise('minecraft:end', { type: str('minecraft:the_end') }),
      }),
    }),
  })
  const gameRules = comp(Object.fromEntries(
    Object.entries(GAMERULES).map(([k, v]) => [k, str(v)])))
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
          GameType: int(GAMEMODES[opts.gamemode] ?? 1),
          allowCommands: byte(1),
          Difficulty: byte(1),
          SpawnX: int(Math.floor(opts.spawn[0])),
          SpawnY: int(Math.floor(opts.spawn[1])),
          SpawnZ: int(Math.floor(opts.spawn[2])),
          SpawnAngle: { type: 'float', value: opts.spawnAngle || 0 },
          WorldGenSettings: worldGen,
          GameRules: gameRules,
          DataPacks: comp({ Enabled: list('string', ['vanilla']), Disabled: list('end', []) }),
          WasModded: byte(0),
          Time: { type: 'long', value: [0, 0] },
          DayTime: { type: 'long', value: [0, 6000] }, // midday
          raining: byte(0),
          thundering: byte(0),
          hardcore: byte(0),
          initialized: byte(1),
          // pre-1.16 readers (and the web client's flying-squid) key on these
          generatorName: { type: 'string', value: 'flat' },
          generatorVersion: int(0),
        },
      },
    },
  }
  const buf = nbt.writeUncompressed(data, 'big')
  fs.writeFileSync(path.join(worldDir, 'level.dat'), zlib.gzipSync(buf))
}

// The widest exterior door whose outside has terrain under it: where a
// visitor would walk in. Returns blocks.csv coords of the standing cell in
// front of it plus the yaw (vanilla degrees, 0 = +Z) that faces the door.
function findEntrance(rows) {
  const at = new Map()
  for (const r of rows) at.set(r.x + ',' + r.y + ',' + r.z, r.block)
  const get = (x, y, z) => at.get(x + ',' + y + ',' + z)
  const isGround = (b) => b && /grass_block|dirt/.test(b)
  const step = { north: [0, -1], south: [0, 1], east: [1, 0], west: [-1, 0] }
  const doors = rows.filter((r) => /_door\[/.test(r.block) && /half=lower/.test(r.block))
  const cands = []
  for (const d of doors) {
    const facing = (d.block.match(/facing=(\w+)/) || [])[1]
    if (!step[facing]) continue
    const [fx, fz] = step[facing]
    for (const sgn of [1, -1]) {
      for (let k = 2; k <= 3; k++) {
        const x = d.x + sgn * fx * k, z = d.z + sgn * fz * k
        if (get(x, d.y, z) || get(x, d.y + 1, z)) continue
        if (!isGround(get(x, d.y - 1, z))) continue
        // the door's run: same-facing lower halves side by side
        let leaves = 1
        for (const s of [1, -1]) {
          for (let i = 1; i < 6; i++) {
            const b = get(d.x + s * fz * i, d.y, d.z + s * fx * i)
            if (b && b.includes('facing=' + facing) && b.includes('half=lower')) leaves++
            else break
          }
        }
        // vanilla yaw: 0 faces +Z, 90 faces -X, 180 faces -Z, 270 faces +X
        const dx = d.x - x, dz = d.z - z
        const yaw = ((Math.atan2(-dx, dz) * 180) / Math.PI + 360) % 360
        cands.push({ x: x + 0.5, y: d.y, z: z + 0.5, yaw, leaves, door: [d.x, d.y, d.z] })
        break
      }
    }
  }
  if (!cands.length) return null
  // widest run first; then the lowest sill (ground floor); then a stable order
  cands.sort((a, b) => b.leaves - a.leaves || a.y - b.y || a.door[0] - b.door[0] || a.door[2] - b.door[2])
  return cands[0]
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

  // Signs: a block plus its block entity. Waxed, so a player cannot edit
  // the room labels by right-clicking them.
  let signsPlaced = 0
  if (opts.signs) {
    const text = (t) => ({ type: 'string', value: JSON.stringify({ text: t }) })
    const face = (lines) => ({ type: 'compound', value: {
      messages: { type: 'list', value: { type: 'string',
        value: [0, 1, 2, 3].map((i) => text(lines[i] || '')).map((t) => t.value) } },
      color: { type: 'string', value: 'black' },
      has_glowing_text: { type: 'byte', value: 0 },
    } })
    for (const sg of JSON.parse(fs.readFileSync(opts.signs, 'utf8'))) {
      const r = shift(sg)
      const b = makeBlock(sg.block)
      if (!b) { unknown++; unknownNames.add(parseState(sg.block).name); continue }
      setWorld(r.x, r.y, r.z, b)
      chunkAt(r.x >> 4, r.z >> 4).setBlockEntity({ x: r.x & 15, y: r.y, z: r.z & 15 }, {
        id: { type: 'string', value: 'minecraft:sign' },
        x: { type: 'int', value: r.x }, y: { type: 'int', value: r.y }, z: { type: 'int', value: r.z },
        is_waxed: { type: 'byte', value: 1 },
        front_text: face(sg.lines), back_text: face(sg.lines),
      })
      signsPlaced++
    }
  }

  // Ground: every column of every saved chunk, plus a margin of chunks
  // around the footprint, layered exactly as the flat generator in level.dat.
  const groundY = opts.baseY - 1
  opts.layers = groundLayers(groundY)
  let floorCount = 0
  if (opts.floor) {
    const margin = 16
    for (let cx = (-margin) >> 4; cx <= ((maxX - minX) + margin) >> 4; cx++) {
      for (let cz = (-margin) >> 4; cz <= ((maxZ - minZ) + margin) >> 4; cz++) chunkAt(cx, cz)
    }
    const column = []
    let y = -64
    for (const l of opts.layers) for (let i = 0; i < l.height; i++) column.push([y++, makeBlock(l.block)])
    for (const c of chunks.values()) {
      for (let x = 0; x < 16; x++) {
        for (let z = 0; z < 16; z++) {
          for (const [yy, b] of column) c.setBlock({ x, y: yy, z }, b)
          floorCount++
        }
      }
    }
  }

  // Spawn: on the ground in front of the main entrance, facing it.
  const entrance = opts.spawn ? null : findEntrance(rows)
  if (opts.spawn) {
    const s = shift({ x: opts.spawn[0], y: opts.spawn[1], z: opts.spawn[2] })
    opts.spawnAngle = opts.spawn.length > 3 ? opts.spawn[3] : 0
    opts.spawn = [s.x, s.y, s.z]
  } else if (entrance) {
    const s = shift(entrance)
    opts.spawn = [s.x, s.y, s.z]
    opts.spawnAngle = entrance.yaw
  } else {
    // no grounded exterior door: fall back to just above the roof
    const buildTop = opts.baseY + (maxY - minY)
    opts.spawn = [Math.floor((maxX - minX) / 2), Math.min(buildTop + 6, 315), Math.floor((maxZ - minZ) / 2)]
  }

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
    signs_placed: signsPlaced,
    ground_layers: opts.layers,
    spawn_angle: opts.spawnAngle || 0,
    spawn_entrance: entrance,
    gamemode: opts.gamemode,
    unknown_blocks: unknown,
    unknown_names: [...unknownNames].sort(),
    spawn: opts.spawn,
    counts: Object.fromEntries([...counts.entries()].sort((a, b) => b[1] - a[1])),
  }
  fs.writeFileSync(path.join(worldDir, 'export_summary.json'), JSON.stringify(summary, null, 2))
  console.log(JSON.stringify(summary, null, 2))
}

main().catch((e) => { console.error(e.stack || String(e)); process.exit(1) })
