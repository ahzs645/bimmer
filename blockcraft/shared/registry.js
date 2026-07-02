// Block / item / entity registries shared by the Node server
// (server/modules/Server.js) and the serverless browser build
// (client/src/offline/LocalServer.js). ORDER MATTERS: a block's numeric id is
// its index + 1, and saved worlds / building.json bake those ids in — keep
// appends at the end.
"use strict";

function buildRegistry() {
  const colors = [
    "black",
    "blue",
    "brown",
    "cyan",
    "gray",
    "green",
    "light_blue",
    "lime",
    "magenta",
    "orange",
    "pink",
    "purple",
    "red",
    "silver",
    "white",
    "yellow",
  ];
  const stoneTypes = ["granite", "andesite", "diorite"];
  const blockOrder = [
    "water",
    "bedrock",
    "stone",
    "dirt",
    "cobblestone",
    "grass",
    "log_oak",
    "leaves_oak",
    "coal_ore",
    "diamond_ore",
    "iron_ore",
    "gold_ore",
    "crafting_table",
    "planks_oak",
    "snow",
    "snowy_grass",
    "ice",
    "ice_packed",
    "sand",
    "sandstone",
    "clay",
    "gravel",
    "obsidian",
    "glowstone",
    "coal_block",
    "iron_block",
    "gold_block",
    "diamond_block",
    "brick",
    "bookshelf",
    "cobblestone_mossy",
    "glass",
    "wool_colored_white",
    "stonebrick",
    "stonebrick_carved",
    "stonebrick_cracked",
    "stonebrick_mossy",
    "furnace",
    "hay_block",
    "tnt",
    "cake",
    "hardened_clay",
    "coarse_dirt",
    "brown_mushroom_block",
    "red_mushroom_block",
    "mushroom_stem",
    "mycelium",
    "emerald_ore",
    "emerald_block",
    "end_stone",
    "jukebox",
    "melon",
    "mob_spawner",
    "prismarine_bricks",
    "red_sand",
    "red_sandstone",
    "red_sandstone_smooth",
    "redstone_block",
    "slime",
    "soul_sand",
    "sponge",
    "sponge_wet",
    "door_x", // bimmer: closed door, panel faces +/-x (solid)
    "door_z", // bimmer: closed door, panel faces +/-z (solid)
    "door_x_open", // bimmer: open door faces +/-x (walk-through)
    "door_z_open", // bimmer: open door faces +/-z (walk-through)
  ];

  const woodTypes = ["spruce", "birch", "jungle", "acacia", "big_oak"];
  for (const type of woodTypes) {
    blockOrder.push("log_" + type);
    blockOrder.push("planks_" + type);
    blockOrder.push("leaves_" + type);
  }

  for (const color of colors) {
    blockOrder.push("wool_colored_" + color);
    blockOrder.push("glass_" + color);
    blockOrder.push("hardened_clay_stained_" + color);
  }

  for (const stoneType of stoneTypes) {
    blockOrder.push("stone_" + stoneType);
    blockOrder.push("stone_" + stoneType + "_smooth");
  }

  const tools = ["pickaxe", "axe", "shovel", "sword"];
  const toolMat = ["wood", "stone", "iron", "gold", "diamond"];
  const armorTypes = ["helmet", "chestplate", "leggings", "boots"];
  const foods = ["beef", "chicken", "porkchop", "mutton", "rabbit"];
  const itemOrder = [
    "bucket_empty",
    "stick",
    "string",
    "bow",
    "arrow",
    "coal",
    "iron_ingot",
    "gold_ingot",
    "gold_nugget",
    "diamond",
    "emerald",
    "leather",
    "apple",
    "apple_golden",
    "bread",
    "carrot",
    "cookie",
    "egg",
    "potato",
    "potato_baked",
    "wheat",
    "clay_ball",
    "flint",
    "flint_and_steel",
    "brick",
    "glowstone_dust",
    "snowball",
    "ender_pearl",
    "fireball",
    "sign",
  ];
  for (const mat of toolMat) {
    for (const tool of tools) {
      itemOrder.push(mat + "_" + tool);
    }
  }
  for (const food of foods) {
    itemOrder.push(food + "_raw");
    itemOrder.push(food + "_cooked");
  }
  for (const armorType of armorTypes) {
    itemOrder.push("leather_" + armorType);
    itemOrder.push("chainmail_" + armorType);
    itemOrder.push("gold_" + armorType);
    itemOrder.push("iron_" + armorType);
    itemOrder.push("diamond_" + armorType);
    itemOrder.push("empty_armor_slot_" + armorType);
  }

  const entityOrder = ["arrow"];

  return { blockOrder, itemOrder, entityOrder };
}

module.exports = { buildRegistry };
