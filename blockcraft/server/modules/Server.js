let fs = require("fs");
const THREE = require("three");
var Filter = require("bad-words"),
  filter = new Filter();

module.exports = class World {
  constructor(options) {
    this.startTime = Date.now();

    this.textures = {};
    this.blockOrder = [];
    this.itemOrder = [];

    this.operators = [];
    this.blacklist = [];

    this.initTextures();

    this.damageChart = {
      wood_sword: 4,
      gold_sword: 4,
      stone_sword: 5,
      iron_sword: 6,
      diamond_sword: 7,
      wood_pickaxe: 2,
      gold_pickaxe: 2,
      stone_pickaxe: 3,
      iron_pickaxe: 4,
      diamond_pickaxe: 5,
      wood_axe: 3,
      gold_axe: 3,
      stone_axe: 4,
      iron_axe: 5,
      diamond_axe: 6,
      wood_shovel: 1,
      gold_shovel: 1,
      stone_shovel: 2,
      iron_shovel: 3,
      diamond_shovel: 4,
    };

    this.armorChart = {
      helmet: [0, 1, 2, 2, 2, 3],
      chestplate: [0, 3, 5, 5, 6, 8],
      leggings: [0, 2, 3, 4, 5, 6],
      boots: [0, 1, 1, 1, 2, 3],
    };
    this.toughnessChart = {
      helmet: [0, 0, 0, 0, 0, 2],
      chestplate: [0, 0, 0, 0, 0, 2],
      leggings: [0, 0, 0, 0, 0, 2],
      boots: [0, 0, 0, 0, 0, 2],
    };
  }

  initTextures() {
    // Block / item / entity orders live in ../../shared/registry.js so the
    // serverless browser build (client/src/offline) uses the exact same ids.
    const { buildRegistry } = require("../../shared/registry");
    const reg = buildRegistry();
    this.blockOrder = reg.blockOrder;
    this.itemOrder = reg.itemOrder;
    this.entityOrder = reg.entityOrder;

    this.blockId = {};
    for (let i = 0; i < this.blockOrder.length; i++) this.blockId[this.blockOrder[i]] = i + 1;

    this.itemId = {};
    for (let i = 0; i < this.itemOrder.length; i++) this.itemId[this.itemOrder[i]] = i + 1;

    this.textures = {};
    let self = this;

    const assetsPath = process.env.TEXTURES_PATH || __dirname + "/../../client/assets/textures";
    // Get block file names
    fs.readdir(assetsPath + "/blocks", function (err, data) {
      if (err) console.log(err);
      self.textures.blocks = data;
    });
    // Get item file names
    fs.readdir(assetsPath + "/items", function (err, data) {
      if (err) console.log(err);
      self.textures.items = data;
    });
    // Get entity file names
    fs.readdir(assetsPath + "/entity", function (err, data) {
      if (err) console.log(err);
      self.textures.entity = data;
    });
    this.textures.blockOrder = this.blockOrder;
    this.textures.itemOrder = this.itemOrder;
    this.textures.entityOrder = this.entityOrder;
    this.textures.tileSize = 16;
    this.textures.tileTextureWidth = 4096;
    this.textures.tileTextureHeight = 64;
  }

  // Get entity by name
  getEntity(name, count) {
    if (this.blockId[name]) {
      return {
        v: this.blockId[name],
        c: count || 1,
        class: "block",
      };
    } else if (this.itemId[name]) {
      return {
        v: this.itemId[name],
        c: count || 1,
        class: "item",
      };
    }
  }

  // Check username profanity
  checkProfanity(username) {
    let isProfane = filter.isProfane(username);
    if (isProfane || !username) {
      username = "Player" + Math.floor(Math.random() * 9999);
    }
    return {
      username: username,
      isProfane: isProfane,
    };
  }

  // Add player
  addPlayer(id, data = {}) {
    let skins = ["steve", "alex", "zombie", "skeleton"];
    if (!skins.includes(data.skin)) data.skin = "steve";

    let player = {
      id: id,
      name: data.name,
      pos: data.pos || { x: 0, y: 0, z: 0 },
      vel: { x: 0, y: 0, z: 0 },
      rot: { x: 0, y: 0, z: 0 },
      dir: { x: 0, y: 0, z: 0 },
      localVel: { x: 0, y: 0, z: 0 },
      hp: 20,
      dead: false,
      toolbar: [
        // Starting items
        this.getEntity("wood_sword"),
        this.getEntity("wood_pickaxe"),
        this.getEntity("wood_axe"),
        this.getEntity("bow"),
        this.getEntity("arrow", 64),
        this.getEntity("ender_pearl", 16),
        this.getEntity("log_oak", 64),
        this.getEntity("iron_ingot", 64),
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
      biome: "",
      operator: this.operators.includes(data.token),
      skin: data.skin,
      armor: {
        helmet: 0,
        chestplate: 0,
        leggings: 0,
        boots: 0,
      },
      immune: Date.now(),
      type: data.type,
      bowCharge: 0,
    };

    return player;
  }

  // Add entity
  addEntity(id, data) {
    let entity = {
      id: id,
      pos: new THREE.Vector3(data.pos.x, data.pos.y, data.pos.z),
      vel: new THREE.Vector3(data.vel.x, data.vel.y, data.vel.z),
      acc: new THREE.Vector3(),
      force: data.force,
      type: "item",
      v: data.v,
      class: data.class || "block",
      playerId: data.playerId,
      name: data.name,
      t: Date.now(),
      onObject: false,
    };

    return entity;
  }

  // Add server operator
  setOperator(fs, isOp, player, token) {
    if (!player) return;

    if (isOp && !this.operators.includes(token)) {
      this.operators.push(token);
      fs.writeFile(__dirname + "/../operators.json", JSON.stringify(this.operators), function (err) {
        if (err) console.log(err);
      });
    } else if (!isOp) {
      for (let i = 0; i < this.operators.length; i++) {
        if (this.operators[i] == token) {
          this.operators.splice(i, 1);
          break;
        }
      }
      fs.writeFile(__dirname + "/../operators.json", JSON.stringify(this.operators), function (err) {
        if (err) console.log(err);
      });
    }
  }

  // Set blacklist
  setBlacklist(fs, ban, player, ip, token) {
    if (!player) return;

    let isBanned = false;
    for (let i = 0; i < this.blacklist.length; i++) {
      if (this.blacklist[i].token == token || this.blacklist[i].ip == ip) {
        isBanned = true;
        break;
      }
    }

    if (ban && !isBanned) {
      this.blacklist.push({
        name: player.name,
        token: token,
        ip: ip,
      });
      fs.writeFile(__dirname + "/../blacklist.json", JSON.stringify(this.blacklist), function (err) {
        if (err) console.log(err);
      });
      return true;
    } else if (!ban) {
      let success = false;
      for (let i = 0; i < this.blacklist.length; i++) {
        if (this.blacklist[i].name == player.name) {
          this.blacklist.splice(i, 1);
          success = true;
          break;
        }
      }
      fs.writeFile(__dirname + "/../blacklist.json", JSON.stringify(this.blacklist), function (err) {
        if (err) console.log(err);
      });
      return success;
    }
    return false; // Already banned
  }

  // Check blacklist
  checkBlacklist(io, socket, check) {
    let blacklisted = false;
    for (let player of this.blacklist) {
      if (player.ip == check || player.token == check) {
        blacklisted = true;
        break;
      }
    }

    if (blacklisted) {
      socket.emit("joinResponse", {
        blacklisted: true,
      });
      io.to(`${socket.id}`).disconnectSockets();
      return;
    }
  }

  // Update players
  updatePlayers(data) {
    let { players, world, logger, io, addLog, bots } = data;
    let regenTick = world.tick % 50 == 0;
    let voidTick = (world.tick + 5) % 10 == 0;

    for (let id in players) {
      let player = players[id];

      // Check if the player is dead
      if (player.hp <= 0 && !player.dead) {
        player.dead = true;
        let txt = player.name;

        if (player.dmgType && player.dmgType.includes("arrow")) {
          let name = player.dmgType.replace("arrow", "");
          txt += " was sniped by " + name;
        } else if (player.dmgType == "drowning") {
          txt += " has drowned";
        } else if (player.dmgType == "fall") {
          txt += " fell off a cliff";
        } else if (player.dmgType == "ender_pearl") {
          txt += " hit the ground too hard";
        } else if (player.dmgType == "void") {
          txt += " fell out of the world";
        } else if (player.dmgType == "explosion") {
          txt += " blew up";
        } else if (player.dmgType == "command") {
          txt += " was killed by a command";
        } else if (player.dmgType) {
          txt += " was slain by " + player.dmgType;
        } else {
          txt += " has died";
        }

        logger.info(txt);

        io.emit("messageAll", {
          text: txt,
        });

        if (bots[id]) {
          // Killed a bot
          delete bots[id];
          io.emit("removePlayer", id);
          delete players[id];
        } else {
          // Killed a player
          addLog(id, "d"); // Deaths
        }
      } else if (!player.dead && player.hp > 0) {
        // Update player armor type
        let armorSlots = player.toolbar.slice(36);
        let armorType = ["helmet", "chestplate", "leggings", "boots"];
        for (let i = 0; i < armorSlots.length; i++) {
          let armor = this.getArmor(armorSlots[i], world);
          if (armor && typeof armor.index == "number" && armorSlots[i].c > 0) {
            player.armor[armor.type] = armor.index + 1;
          } else {
            player.armor[armorType[i]] = 0;
          }
        }

        // Update player position in biome
        player.biome = world.generator.getColumnInfo(player.pos.x / world.blockSize, player.pos.z / world.blockSize)[2];

        // Regenerate health
        if (regenTick) players[id].hp = Math.min(players[id].hp + 1, 20);

        // Check if the player is in the void
        if (voidTick && player.pos.y < -world.blockSize * 64) {
          player.hp -= 2;
          player.dmgType = "void";
          io.to(`${player.id}`).emit("damage");
        }
      }
    }
  }

  getArmor(item, world) {
    if (!item) return;

    let armorType = ["helmet", "chestplate", "leggings", "boots"];
    let armorMat = ["leather", "gold", "chainmail", "iron", "diamond"];
    let correctType = false;
    for (let i = 0; i < armorType.length; i++) {
      let type = armorType[i];
      for (let j = 0; j < armorMat.length; j++) {
        let mat = armorMat[j];
        if (item.v == world.itemId[`${mat}_${type}`]) {
          correctType = true;
          return {
            type: type,
            index: j,
          };
        }
      }
    }
  }
};
