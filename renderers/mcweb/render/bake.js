// bake.js — resolve real vanilla block models (minecraft-assets) into a flat
// list of cuboids for a headless three.js render. Proves the renderer draws our
// exported block-states as correct SHAPES (thin door, stepped stairs, half slab,
// post-and-rail fence), not cubes. Run: node bake.js  (writes scene.json)
const fs = require('fs')
const a = require('minecraft-assets')('1.20.2')

function model(name){ name=name.replace('minecraft:',''); return a.blocksModels[name]||a.blocksModels[name.replace('block/','')]||a.blocksModels['block/'+name] }
function elements(name){ let m=model(name),els=null,g=0; while(m&&g++<10){ if(m.elements&&!els) els=m.elements; if(!m.parent)break; m=model(m.parent)} return els||[] }

// Return the applicable model parts [{model,x,y}] for a block-state, handling
// both `variants` (door/stairs/slab) and `multipart` (fence) blockstates.
function partsFor(block, props){
  const st=a.blocksStates[block]; if(!st) return []
  if(st.variants){
    for(const key of Object.keys(st.variants)){
      const conds=key===''?[]:key.split(',')
      if(conds.every(c=>{const [k,v]=c.split('=');return String(props[k])===v})){
        let v=st.variants[key]; if(Array.isArray(v))v=v[0]; return [v]
      }
    }
    return []
  }
  if(st.multipart){
    const out=[]
    for(const part of st.multipart){
      const when=part.when
      let ok=!when
      if(when){
        ok=Object.entries(when).every(([k,v])=>String(v).split('|').includes(String(props[k])))
      }
      if(ok){ let ap=part.apply; if(Array.isArray(ap))ap=ap[0]; out.push(ap) }
    }
    return out
  }
  return []
}
function parse(str){ const m=str.match(/^([^\[]+)(?:\[(.*)\])?$/); const name=m[1].replace('minecraft:',''); const props={}; if(m[2])for(const kv of m[2].split(',')){const [k,vv]=kv.split('=');props[k]=vv} return {name,props} }

// COLORS per base block (flat-shaded; shape is what we're proving)
const COLOR={ white_concrete:'#dcdcdc', oak_door:'#9a7b4f', stone_brick_stairs:'#8b8b8b',
  smooth_stone_slab:'#a6a6a6', light_blue_stained_glass:'#66aadd', oak_fence:'#9a7b4f' }

// the demo row: each entry -> a block-state string placed at grid x.
// (fence gets north|south connections so it reads as a rail run, not a lone post.)
const placements=[
  ['minecraft:white_concrete', [0,0,0]],
  ['minecraft:light_blue_stained_glass', [2,0,0]],
  ['minecraft:oak_door[facing=north,half=lower,hinge=left,open=false]', [4,0,0]],
  ['minecraft:oak_door[facing=north,half=upper,hinge=left,open=false]', [4,1,0]],
  ['minecraft:smooth_stone_slab[type=top]', [6,0,0]],
  ['minecraft:oak_fence[east=true,west=true]', [8,0,0]],
  // a real staircase run (facing west = ascending toward -x), exactly what the
  // voxelizer's stair refinement emits from a stepped ramp:
  ['minecraft:stone_brick_stairs[facing=west,half=bottom,shape=straight]', [12,0,0]],
  ['minecraft:stone_brick_stairs[facing=west,half=bottom,shape=straight]', [11,1,0]],
  ['minecraft:stone_brick_stairs[facing=west,half=bottom,shape=straight]', [10,2,0]],
]

const scene=[]
for(const [str,pos] of placements){
  const {name,props}=parse(str)
  const parts=partsFor(name,props)
  if(!parts.length){ console.error('no model for',str); continue }
  const els=[]
  for(const p of parts) for(const e of elements(p.model)) els.push({from:e.from,to:e.to,yRot:p.y||0,xRot:p.x||0})
  scene.push({ block:name, color:COLOR[name]||'#cccccc', opacity: name.includes('glass')?0.55:1, pos, elements:els })
  console.error(`baked ${str} -> ${parts.map(p=>p.model.replace('minecraft:block/','')).join('+')} (${els.length} cuboids)`)
}
fs.writeFileSync(require('path').join(__dirname,'scene.json'), JSON.stringify(scene,null,1))
console.error('wrote scene.json with', scene.length, 'blocks')
