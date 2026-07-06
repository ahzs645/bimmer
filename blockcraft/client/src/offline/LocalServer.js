/*
 * LocalServer — a serverless, in-browser replacement for the BlockCraft Node
 * server, so the client runs as a fully STATIC site (e.g. GitHub Pages).
 *
 * It emulates the exact socket.io surface the client uses (connect / join /
 * requestChunk / packet / setBlock / ...) against a local world: the same
 * flat-world + building generator the patched server uses
 * (server/modules/WorldGeneration.js), the same RLE chunk encoding, and the
 * same block registry (../../shared/registry.js — ids must match
 * building.json). The building is fetched from `building.json` next to
 * index.html.
 *
 * Single player only: no other players, no entities, no persistence.
 */
import RLE from "../../../server/modules/RLE.js";
import { buildRegistry } from "../../../shared/registry.js";

const TICK_MS = 50;

// Texture file manifests, resolved AT BUILD TIME by webpack (the Node server
// fs.readdir()s these directories at runtime; a static site can't).
const listNames = (ctx) => ctx.keys().map((k) => k.replace("./", ""));
const textureFiles = {
  blocks: listNames(require.context("../../assets/textures/blocks", false, /\.(png|jpe?g)$/)),
  items: listNames(require.context("../../assets/textures/items", false, /\.(png|jpe?g)$/)),
  entity: listNames(require.context("../../assets/textures/entity", false, /\.(png|jpe?g)$/)),
};

class LocalWorld {
  constructor(registry) {
    this.blockSize = 16;
    this.cellSize = 16;
    this.buildHeight = this.cellSize * 8;
    this.cellSliceSize = this.cellSize * this.cellSize;
    this.cells = {};
    this.cellDeltas = {};
    this.entities = {};
    this.newEntities = [];
    this.updatedBlocks = [];
    this.tick = 0;
    this.seed = 0.5;
    this.canUpdate = true;

    this.blockOrder = registry.blockOrder;
    this.itemOrder = registry.itemOrder;
    this.blockId = {};
    this.blockIdLegit = {};
    for (let i = 0; i < this.blockOrder.length; i++) {
      this.blockId[this.blockOrder[i]] = i + 1;
      if (i > 1) this.blockIdLegit[this.blockOrder[i]] = i + 1;
    }
    this.blockOrderLegit = this.blockOrder.slice(2);
    this.itemId = {};
    for (let i = 0; i < this.itemOrder.length; i++) this.itemId[this.itemOrder[i]] = i + 1;

    // flat world + building (set by loadBuilding)
    this.groundY = 4;
    this.buildingCells = {};
    this.buildingMeta = null;
  }

  static euclideanModulo(a, b) {
    return ((a % b) + b) % b;
  }
  computeVoxelOffset(x, y, z) {
    const { cellSize, cellSliceSize } = this;
    const vx = LocalWorld.euclideanModulo(x, cellSize) | 0;
    const vy = LocalWorld.euclideanModulo(y, cellSize) | 0;
    const vz = LocalWorld.euclideanModulo(z, cellSize) | 0;
    return vy * cellSliceSize + vz * cellSize + vx;
  }
  computeCellId(x, y, z) {
    const { cellSize } = this;
    return `${Math.floor(x / cellSize)},${Math.floor(y / cellSize)},${Math.floor(z / cellSize)}`;
  }
  getCellForVoxel(x, y, z, cellDelta) {
    const id = this.computeCellId(x, y, z);
    return cellDelta ? this.cellDeltas[id] : this.cells[id];
  }
  addCellForVoxel(x, y, z) {
    const id = this.computeCellId(x, y, z);
    const n = Math.pow(this.cellSize, 3);
    if (!this.cells[id]) this.cells[id] = new Uint8Array(n);
    if (!this.cellDeltas[id]) this.cellDeltas[id] = new Uint8Array(n);
    return this.cells[id];
  }
  setVoxel(x, y, z, v, changeDelta, addCell = true) {
    let cell = this.getCellForVoxel(x, y, z);
    if (!cell) {
      if (!addCell) return;
      cell = this.addCellForVoxel(x, y, z);
    }
    const off = this.computeVoxelOffset(x, y, z);
    cell[off] = v;
    if (changeDelta) {
      let delta = this.getCellForVoxel(x, y, z, true);
      if (!delta) {
        this.addCellForVoxel(x, y, z);
        delta = this.getCellForVoxel(x, y, z, true);
      }
      delta[off] = v + 1;
    }
  }
  getVoxel(x, y, z, cellDelta) {
    const cell = this.getCellForVoxel(x, y, z, cellDelta);
    if (!cell) return 0;
    return cell[this.computeVoxelOffset(x, y, z)];
  }
  encodeCell(cellX, cellY, cellZ) {
    const { cellSize } = this;
    return RLE.encode(this.getCellForVoxel(cellX * cellSize, cellY * cellSize, cellZ * cellSize));
  }

  async loadBuilding(url) {
    let data = null;
    try {
      const res = await fetch(url);
      if (res.ok) data = await res.json();
    } catch (e) {
      /* no building — plain flat world */
    }
    if (!data) {
      console.warn("[offline] no building.json — generating a plain flat world");
      this.buildingMeta = {};
      return;
    }
    this.buildingMeta = data;
    if (typeof data.ground_y === "number") this.groundY = data.ground_y;
    const cs = this.cellSize;
    let placed = 0;
    let skipped = 0;
    for (const name in data.blocks) {
      const id = this.blockId[name];
      if (!id) {
        skipped += data.blocks[name].length;
        continue;
      }
      for (const pos of data.blocks[name]) {
        const key = Math.floor(pos[0] / cs) + "," + Math.floor(pos[1] / cs) + "," + Math.floor(pos[2] / cs);
        (this.buildingCells[key] || (this.buildingCells[key] = [])).push([pos[0], pos[1], pos[2], id]);
        placed++;
      }
    }
    console.log(`[offline] loaded building: ${placed} blocks (skipped ${skipped} unknown), groundY=${this.groundY}`);
  }

  // Same layering as the patched server generator: flat ground, stamped
  // building, then player edits (deltas) on top.
  generateCell(cellX, cellY, cellZ) {
    const { cellSize } = this;
    const G = this.groundY;
    const bedrock = this.blockId["bedrock"];
    const dirt = this.blockId["dirt"];
    const grass = this.blockId["grass"];

    for (let x = 0; x < cellSize; ++x) {
      for (let z = 0; z < cellSize; ++z) {
        const xPos = x + cellX * cellSize;
        const zPos = z + cellZ * cellSize;
        for (let y = 0; y < cellSize; ++y) {
          const yPos = y + cellY * cellSize;
          let id = 0;
          if (yPos === 0) id = bedrock;
          else if (yPos < G) id = dirt;
          else if (yPos === G) id = grass;
          if (id) this.setVoxel(xPos, yPos, zPos, id);
        }
      }
    }

    const blocks = this.buildingCells[cellX + "," + cellY + "," + cellZ];
    if (blocks) {
      for (let i = 0; i < blocks.length; i++) {
        this.setVoxel(blocks[i][0], blocks[i][1], blocks[i][2], blocks[i][3]);
      }
    }

    for (let z = 0; z < cellSize; ++z) {
      for (let x = 0; x < cellSize; ++x) {
        for (let y = 0; y < cellSize; ++y) {
          const xPos = x + cellX * cellSize;
          const yPos = y + cellY * cellSize;
          const zPos = z + cellZ * cellSize;
          const v = this.getVoxel(xPos, yPos, zPos, true) - 1;
          if (v >= 0) this.setVoxel(xPos, yPos, zPos, v, false, true);
        }
      }
    }
  }
}

export function createLocalSocket(options = {}) {
  const registry = buildRegistry();
  const world = new LocalWorld(registry);
  const buildingUrl = options.buildingUrl || "building.json";
  const id = "offline-player";

  const listeners = {};
  let player = null;
  let tickTimer = null;
  let ready = null; // building fetch promise

  const socket = {
    id,
    connected: false,
    // the client assigns g.socket.io.uri before connect() and subscribes to
    // manager-level reconnect events — accept both, reconnects never happen
    io: {
      uri: "offline",
      on() {
        return this;
      },
    },

    on(event, cb) {
      (listeners[event] || (listeners[event] = [])).push(cb);
      return socket;
    },
    once(event, cb) {
      const wrap = (...a) => {
        listeners[event] = (listeners[event] || []).filter((f) => f !== wrap);
        cb(...a);
      };
      return socket.on(event, wrap);
    },

    connect() {
      if (socket.connected) return socket;
      socket.connected = true;
      ready = ready || world.loadBuilding(buildingUrl);
      fire("connect");
      fire("textureData", {
        blocks: textureFiles.blocks,
        items: textureFiles.items,
        entity: textureFiles.entity,
        blockOrder: world.blockOrder,
        itemOrder: world.itemOrder,
        entityOrder: registry.entityOrder,
        tileSize: 16,
        tileTextureWidth: 4096,
        tileTextureHeight: 64,
      });
      return socket;
    },

    disconnect() {
      if (!socket.connected) return socket;
      socket.connected = false;
      if (tickTimer) clearInterval(tickTimer);
      tickTimer = null;
      player = null;
      // fresh world on the next connect (mirrors joining a restarted server)
      world.cells = {};
      world.cellDeltas = {};
      world.updatedBlocks.length = 0;
      fire("disconnect", "io client disconnect");
      return socket;
    },

    emit(event, data) {
      handlers[event] && handlers[event](data);
      return socket;
    },
  };

  function fire(event, ...args) {
    for (const cb of listeners[event] || []) {
      setTimeout(() => cb(...args), 0);
    }
  }

  function makePlayer(data = {}) {
    const getEntity = (name, count) => {
      if (world.blockId[name]) return { v: world.blockId[name], c: count || 1, class: "block" };
      if (world.itemId[name]) return { v: world.itemId[name], c: count || 1, class: "item" };
    };
    return {
      id,
      name: data.name || "Explorer",
      pos: { x: 0, y: 0, z: 0 },
      vel: { x: 0, y: 0, z: 0 },
      rot: { x: 0, y: 0, z: 0 },
      dir: { x: 0, y: 0, z: 0 },
      localVel: { x: 0, y: 0, z: 0 },
      hp: 20,
      dead: false,
      toolbar: [
        getEntity("wood_sword"),
        getEntity("wood_pickaxe"),
        getEntity("wood_axe"),
        getEntity("bow"),
        getEntity("arrow", 64),
        getEntity("ender_pearl", 16),
        getEntity("log_oak", 64),
        getEntity("iron_ingot", 64),
      ],
      walking: false,
      sneaking: false,
      punching: false,
      currSlot: 0,
      pickupDelay: Date.now(),
      ping: [],
      connected: true,
      mode: "creative",
      fps: 0,
      showInventory: false,
      biome: "GRASSLAND",
      operator: true,
      skin: ["steve", "alex"].includes(data.skin) ? data.skin : "steve",
      armor: { helmet: 0, chestplate: 0, leggings: 0, boots: 0 },
      immune: Date.now(),
      type: data.type,
      bowCharge: 0,
    };
  }

  function worldSnapshot() {
    return {
      blockSize: world.blockSize,
      cellSize: world.cellSize,
      cellSliceSize: world.cellSliceSize,
      buildHeight: world.buildHeight,
      seed: world.seed,
      tick: world.tick,
      canUpdate: world.canUpdate,
      blockOrder: world.blockOrder,
      blockId: world.blockId,
      blockIdLegit: world.blockIdLegit,
      blockOrderLegit: world.blockOrderLegit,
      itemOrder: world.itemOrder,
      itemId: world.itemId,
      entities: {},
      cells: {},
    };
  }

  const handlers = {
    join: async (data) => {
      await ready;
      player = makePlayer(data || {});

      // Spawn hovering above the building centre (building.json is centred on
      // the origin), like the multiplayer fork does; fall back to the flat
      // ground when there is no building.
      const meta = world.buildingMeta || {};
      const dims = meta.dims || [0, 0, 0];
      const baseY = typeof meta.base_y === "number" ? meta.base_y : world.groundY + 1;
      const hoverY = dims[1] ? baseY + dims[1] + 10 : world.groundY + 3;
      fire("joinResponse", {
        serverPlayers: { [id]: player },
        world: worldSnapshot(),
        tick: world.tick,
        startPos: {
          x: 0,
          y: hoverY * world.blockSize,
          z: 0,
        },
        info: { region: "offline", link: null, port: null },
        operator: true,
        name: player.name,
      });
      fire("addPlayer", player);
      fire("messageAll", {
        text: "Serverless mode: the world runs entirely in your browser.",
        color: "aqua",
      });

      if (!tickTimer) {
        tickTimer = setInterval(() => {
          if (!player) return;
          world.tick += 1;
          const update = {
            serverPlayers: { [id]: player },
            updatedBlocks: world.updatedBlocks,
            newEntities: [],
            entities: {},
            tick: world.tick,
            t: Date.now(),
            tps: TICK_MS,
          };
          fire("update", JSON.stringify(update));
          world.updatedBlocks = [];
        }, TICK_MS);
      }
    },

    requestChunk: async (data) => {
      await ready;
      const out = [];
      for (const chunk of data || []) {
        const cid = `${chunk.x},${chunk.y},${chunk.z}`;
        if (!world.cells[cid]) {
          world.cells[cid] = new Uint8Array(Math.pow(world.cellSize, 3));
          world.generateCell(chunk.x, chunk.y, chunk.z);
        }
        out.push({ pos: chunk, cell: world.encodeCell(chunk.x, chunk.y, chunk.z), cellSize: world.cellSize });
      }
      fire("receiveChunk", out);
    },

    packet: (data) => {
      if (player) Object.assign(player, data);
    },

    setBlock: (data) => {
      if (!data || data.y === 0) return;
      world.setVoxel(data.x, data.y, data.z, data.t, true, true);
      // loop back through the tick update so ChunkManager remeshes the cell
      world.updatedBlocks.push(data);
    },

    updateInventory: (data) => {
      if (player) player.toolbar = data;
    },

    message: (text) => {
      // no server commands offline — just echo the chat line
      if (typeof text === "string" && text.length) {
        fire("messageAll", { name: player ? player.name : "You", text });
      }
    },

    respawn: () => {
      if (player) {
        player.hp = 20;
        player.dead = false;
      }
    },

    updateUsername: (data) => {
      if (player && data && data.name) player.name = data.name;
    },

    // harmless no-ops in single-player offline mode
    latency: () => {},
    dropItems: () => {},
    clearHand: () => {},
    clearInventory: () => {},
    giveItem: () => {},
    takeDamage: () => {},
    fireArrow: () => {},
    throwItem: () => {},
    punchPlayer: () => {},
    messagePlayer: () => {},
    replyPlayer: () => {},
    saveWorld: () => {},
    spawnBot: () => {},
    settime: () => {},
    setOperator: () => {},
    banPlayer: () => {},
    sessionInfoRequest: () => {},
    serverInfoRequest: () => {},
  };

  return socket;
}
