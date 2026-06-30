// PATCHED for the bimmer IFC->Minecraft project.
// Replaces procedural terrain with a FLAT world and stamps a static building
// loaded from server/building.json (produced by scripts/export_blockcraft.py).
// Drop this over blockcraft/server/modules/WorldGeneration.js (scripts/setup_blockcraft.py does this).

const fs = require("fs");
const path = require("path");
let SimplexNoise = require("simplex-noise");

let rng1, rng2;

module.exports = class WorldGeneration {
  constructor(seed) {
    this.heightNoise = 128;
    this.waterLevel = Math.floor(0.1 * this.heightNoise) + 30;
    this.mountainLevel = 80;

    // Flat-world grass level and lazily-loaded building (keyed by cell id).
    this.groundY = parseInt(process.env.FLAT_GROUND_Y || "4", 10);
    this.building = null;
    this.buildingMeta = null;

    if (seed) this.setSeed(seed);
  }

  setSeed(seed) {
    // Kept for API compatibility (worker calls generator.setSeed). Guarded so a
    // simplex-noise version mismatch can't break the flat generator.
    try {
      rng1 = new SimplexNoise(seed);
      rng2 = new SimplexNoise(seed + 0.2 > 1 ? seed - 0.8 : seed + 0.2);
    } catch (e) {
      rng1 = rng2 = null;
    }
  }

  // Flat-world stubs kept for API compatibility. Server.js uses getColumnInfo()[2]
  // only to label the player's biome, and getHeight for spawn estimates.
  getColumnInfo(xPos, zPos) {
    return [0.3, 0.5, "GRASSLAND"];
  }
  biome(e, m) {
    return "GRASSLAND";
  }
  getHeight(height) {
    return this.groundY;
  }

  // Load building.json once and bucket blocks by cell id, mapping block names
  // to BlockCraft numeric ids via world.blockId (available after world.init).
  ensureBuilding(world) {
    if (this.building !== null) return;
    this.building = {};
    const p = path.join(__dirname, "..", "building.json");
    let raw;
    try {
      raw = fs.readFileSync(p, "utf8");
    } catch (e) {
      this.buildingMeta = {};
      console.log("[bimmer] no building.json found at", p);
      return;
    }
    const data = JSON.parse(raw);
    this.buildingMeta = data;
    if (typeof data.ground_y === "number") this.groundY = data.ground_y;

    const cs = world.cellSize;
    let placed = 0,
      skipped = 0;
    for (const name in data.blocks) {
      const id = world.blockId[name];
      if (!id) {
        skipped += data.blocks[name].length;
        continue;
      }
      for (const pos of data.blocks[name]) {
        const x = pos[0],
          y = pos[1],
          z = pos[2];
        const key = Math.floor(x / cs) + "," + Math.floor(y / cs) + "," + Math.floor(z / cs);
        (this.building[key] || (this.building[key] = [])).push([x, y, z, id]);
        placed++;
      }
    }
    console.log(`[bimmer] loaded building: ${placed} blocks (skipped ${skipped} unknown), groundY=${this.groundY}`);
  }

  generateCell(cellX, cellY, cellZ, world, exists) {
    this.ensureBuilding(world);

    const { cellSize } = world;
    const G = this.groundY;
    const bedrock = world.blockId["bedrock"];
    const dirt = world.blockId["dirt"];
    const grass = world.blockId["grass"];

    // 1. Flat ground: bedrock at y=0, dirt below grass, grass at y=G, air above.
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
          if (id) world.setVoxel(xPos, yPos, zPos, id);
        }
      }
    }

    // 2. Stamp the building blocks that belong to this cell.
    const blocks = this.building[cellX + "," + cellY + "," + cellZ];
    if (blocks) {
      for (let i = 0; i < blocks.length; i++) {
        const b = blocks[i];
        world.setVoxel(b[0], b[1], b[2], b[3]);
      }
    }

    // 3. Apply player block edits (cell deltas) on top, as in the original.
    for (let z = 0; z < cellSize; ++z) {
      for (let x = 0; x < cellSize; ++x) {
        for (let y = 0; y < cellSize; ++y) {
          const xPos = x + cellX * cellSize;
          const yPos = y + cellY * cellSize;
          const zPos = z + cellZ * cellSize;
          const v = world.getVoxel(xPos, yPos, zPos, true) - 1;
          if (v >= 0) world.setVoxel(xPos, yPos, zPos, v, false, true);
        }
      }
    }
  }
};
