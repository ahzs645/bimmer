# Lessons: making an IFC → Minecraft conversion actually walkable

This documents what we learned hardening the pipeline on the UNBC campus
model (IFC2X3, ~41k elements, 13 storeys, 1 m/block), how each problem was
fixed, what the player experiences in the deployed world, and what is still
open. Every fix below was verified twice: statistically over all elements
(`scripts/verify_blocks.py`) and visually by teleporting through the actual
minecraft-web-client renderer (~120 doors, stairwells, terraces reviewed
across three in-client sweeps).

The core insight behind almost every fix: **the IFC is the authority, but
voxel rounding lies about it locally.** Meshes round into neighbouring cells,
thin geometry vanishes, and "look at the blocks nearby" heuristics anchor to
the wrong thing. The stable recipe is to (1) trust the IFC's semantic fields
(sill height, overall width, aggregation, shape type) over raw voxel
neighbourhoods, (2) constrain any local probe by that semantic prior, and
(3) verify in the real renderer, because several bugs (hoisted doors, fence
towers) were invisible in statistics that shared the same wrong assumption.

---

## Failure catalog: symptom → root cause → fix

All fixes live in `scripts/ifc_to_voxels.py` unless noted.

### Doors

| # | Symptom (what you saw in game) | Root cause | Fix |
|---|---|---|---|
| D1 | Doors hoisted onto roof decks / terraces, rows of free-standing doors on the roof (~1,200 doors) | Floor probe anchored the door to the *highest* walkable surface nearby — beside a facade that surface is the deck on top of the wall | Anchor to the walkable surface **closest to the IFC sill height**, hard-capped at ±2 cells; beyond that the sill wins outright (`sill_bottom`) |
| D2 | Entrance doors standing in blown-out holes; glazing/sidelights missing around them | Carve step cleared the door's whole bounding box — curtain-wall doors carry metres of glass side panels in their bbox | Carve **only the passage**: leaf columns × wall depth × door height (pass 2a) |
| D3 | A whole door family (~250 doors) placed a storey too high with plugged doorways | Wall-normal guessed from mesh extents; deep-framed families are thinner along the *wrong* axis, so probes ran along the wall and found only the wall top | Decide the normal by probing which axis has **open room cells at sill level**; mesh extents only break ties |
| D4 | Doors standing in the corridor beside a hole in the wall (~140 doors) | Leaf placed at the bbox *middle* depth cell; deep frames span 2+ cells and the middle can be proud of the wall | Score each depth cell by solid wall flanking the opening; place the leaf in the **wall plane** |
| D5 | Adjacent doors on one wall at heights one block apart (17 of 20 pairs) | Independent floor probes diverged (slab edge rounds a cell higher beside one door) even though the IFC sills were identical | **Harmonize**: nearby same-wall doors with the same IFC sill snap to one bottom |
| D6 | Corrupt door columns (`lower,lower,upper`), odd total door-block counts | Two overlapping IfcDoors at one opening resolved to bottoms 1 apart; the later lower overwrote the earlier upper | One shared bottom per opening + a cleanup pass that removes unpaired halves (pass 2.5) |
| D7 | Double doors with leaves at different heights or un-mirrored hinges | Each leaf probed its own floor; runs not detected across separate IfcDoor elements | One floor level per door element; hinge mirroring over runs of adjacent same-facing leaves (pass 3) |
| D8 | Storefront door standing alone in the plaza, glazing starts one cell above | Curtain-wall glazing bay thinner than a voxel at door level — nothing voxelized beside the leaf | Two-tier anchor (pass 2c): pull glazing **down** when it exists above the flanking cell; for doors free on **both** sides, **bridge with glass** to the nearest solid within 3 cells. One-sided doors untouched (open side is usually a passage) |
| D9 | Door floating over a hole, or sunk half into a thick landing | Carve removed the slab under the leaf; landings 2 cells thick | Threshold block under empty leaf cells; probe window covers ±2 around the sill |
| D10 | **Dead-end doors**: open the door and there is bare concrete behind it (~500 doors) | Pass 2a carves only the door's own bbox depth; walls thicker than the frame — and small rooms whose far wall rounds into the doorway — leave 1–3 solid cells in front of the leaf | `unblock_door_passages`: probe outward along the facing normal from every leaf; a short (≤3 m) run of carveable solid that ends at a 2-high open cell with a standable floor is carved door-height tall. Glass, railings, stairs and other doors are never carved, and openings with no floor (exterior drops) are left alone |
| D11 | Doors left **free-standing in the hallway** (wall beside them gone) after the D10 carve | A plug cell in front of door A can simultaneously be the **flanking wall** of perpendicular neighbour door B — carving A's passage strips B's wall | Never carve a plug cell that is laterally adjacent to another door's leaf (both D10 and D12 honour this) |
| D12 | Door flush in the hallway wall, but the small room it serves is **sealed shut behind the wall** (hidden room, no way in) | The whole room-side passage — sometimes the room's entire near wall zone — rounds solid; a ≤3 m carve stops short, and tunnelling blindly deeper would puncture unrelated rooms | `connect_hidden_rooms`: label all enclosed air components (scipy 6-conn); components that never reach the outside, touch **no** door, and contain a standable cell are *hidden rooms*. A door side still plugged may carve up to ~6 m **only when the run ends inside a hidden room**. On this model: 551 hidden pockets found, 16 connected through their hallway door; the rest have no door within reach (mostly inter-floor voids and shafts — invisible in game) |

Result on this model: 2,085 doors — 0 stepped pairs, 0 orphan halves,
0 un-mirrored double doors, 1,610 with a walkable approach on **both** sides
(up from 1,105 before the dead-end pass). The rest are honest artifacts of
1 m voxels, not placement bugs: 72 fully entombed inside thick wall mass
(invisible in game), ~256 closet/shaft doors whose room was swallowed by
wall rounding (open onto solid >3 m deep — carving further would tunnel into
unrelated rooms), and ~147 facade/terrace doors with a genuine drop or
missing exterior floor on one side. All are pin-able via `--overrides`.

### Stairs and railings

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| S1 | Spiral staircase vanished inside its shaft | Walls outranked stairs in `CLASS_PRIORITY`; the outer tread ring shares cells with the shaft wall ring at 1 m | Stairs now beat **everything** solid — the walking path is the point |
| S2 | Spiral voxelization a jumpy blob even when present | Helical treads stack in tight columns; refinement can't orient them | **Synthesize** SPIRAL_STAIR assemblies (`synth_spiral_stairs`): newel + one tread per ring cell, start/end angles + winding measured from the mesh, ≥1 ring cell per rise → walkable with zero jumps |
| S3 | Stair blocks facing the wrong way near walls (~185) | Any raised neighbour counted as "uphill", including walls and fences | Rises only count **stair-class** neighbours |
| S4 | Notched corners on winding stairs | `shape=straight` stored for all stairs; saved worlds never recompute shape | Vanilla corner-shape algorithm (`inner/outer_left/right`) baked into the block state |
| S5 | Fence towers crowding stairwells, flights unclimbable | The guardrail line rounds **onto the tread cells**; fences collide 1.5 blocks tall; plus stacked fence cells from the ~1.1 m rail height | Fences never stand on stair blocks (624 removed); vertical fence stacks collapse to the bottom cell (railings are **one** block high, the vanilla idiom) |
| S6 | Railings render as rows of disconnected posts | Fence arms are stored block-state properties; nothing triggers the in-game neighbour update in a pasted/loaded world | `refine_fences` writes explicit `oak_fence[north/east/south/west=…]` states |
| S7 | Stair stringers rendered as curtain-wall concrete | `IfcMember` is both mullion and stringer | Disambiguate by aggregation: members decomposing an `IfcStair`/`IfcRamp` are stair class |
| S8 | **Every floor above the ground storey unreachable on foot** (0% of interior cells above y≈11) | Two compounding 1 m rounding effects: stairwell slab holes round **shut** (the landing slab plates over the flight), and scissor/dog-leg flights cross inside one shaft, voxelizing into a pile with no 2-cell headroom anywhere | Two passes: `carve_stair_headroom` re-opens floor/roof cells 1–3 above every stair block; `rebuild_blocked_stairs` merges overlapping stair assemblies into wells, **climb-tests** each well (8-dir BFS), and rebuilds only the failing ones as a clean switchback run of oriented stair blocks — never touching door cells or thresholds |
| S9 | **Fire-escape towers replaced by a floating 45° stair ribbon zigzagging across the facade** | Three stacked bugs in the rebuild: (1) wells were merged on mere bbox *contact*, so a stair tower chained with the entrance steps + ramp that touch its box into one long well, and the run marched across the whole facade; (2) the run column defaulted to the well's **middle** lane — for a glazed stair tower that is the curtain-wall/door plane, threading the run through every storey exit door; (3) at door cells the rise was *skipped* (never overwrite a door), leaving 2-cell jumps no player can climb | (1) merge only on genuine horizontal-footprint overlap (scissor pairs and per-storey tower assemblies still merge; edge-contact neighbours don't); (2) pick the **least-obstructed lane** (score doors heavily, protected mass lightly); (3) cross door thresholds **flat** — don't rise on a door cell. All three fire towers now climb ground→top in the final-state BFS |
| S10 | **Rebuilt wells still unclimbable in game**: run present but you stall on the first step; terraced lobby stair needs impossible jumps; glazed towers block mid-flight | Three more holes in the rebuild: (1) the rebuilt run's headroom pass cleared floors/roofs but **not walls** — a run piercing a rounded wall line kept concrete 1–2 cells above its treads; (2) in the glazed fire-escape towers the **shaft glazing** rounds inward directly over the run and a pane above a tread blocks like concrete; (3) the climb test let you "step up" onto full cubes without jump clearance, so cube-terraced stairs (curved lobby stair) passed the test while vanilla physics blocks them | Rebuilt-run headroom now clears wall/structure/mullion **and glass** cells straight above placed treads (a couple of interior panes, not the facade); the climb test and its post-rebuild verify are **jump-aware** (stepping onto an oriented stair block = walk, 2-air suffices; onto a cube = jump, needs clearance above the head), and every rebuild re-verifies itself and warns instead of failing silently. Result: **34 of 36 storey-connecting wells climb and connect**; interior reachability 84% (the 2 remaining wells are climbable but sit in annex wings disconnected at their base — a terrain/wing-seam issue, not a stair defect) |

| S11 | Whole floor areas walkable internally but **unreachable from the entrance** — annex wings linked only through missing site terrain, and (with `--rectify`) wing seams sheared by the rotation | The model has no ground outside the building, and rigid wing rotation moves the wing side of every seam corridor by up to a few metres | `stitch_seams`: BFS the walkable graph from the entrance, component-label the unreachable floor islands, and synthesize the shortest 1×3 corridor from each to the reachable set (floor pads across air gaps; never through doors or a stairwell's climbing envelope), re-running the BFS after each link. Baseline reachability 84 % → **94 %** (all 36 wells pass); rectified 96 %. `scripts/audit_walkability.py` runs the acceptance audit on any blocks.csv |
| S12 | **Floor plates full of potholes** — 1–2-cell holes in otherwise continuous floors/ceilings/roof decks (~3,900 cells on this model): missing ceiling patches overhead, pits you can fall through | Surface voxelization of non-watertight slab meshes drops cells at thin spots, plate joints and penetrations | `patch_floor_holes`: fill support under any air cell whose 8-neighbour ring is ≥6/8 solid at that level (a pothole in a plate — real atria, light wells and facade edges never reach 6/8), skipping stairwell envelopes **and any fill with a floor within 2 cells below** (the first attempt filled ceiling holes of 2-high rooms and silently destroyed ~3,700 walkable cells — the reachability audit caught it). Sky-open roof holes over 2-high plenums get **glass covers** instead (skylight look, no headroom crushed). Interior fall hazards after: ~1 per build. NB: not every dark patch is a hole — the roof reads dark because it *is* dark (`deepslate_tiles`) |
| S13 | **Build suddenly takes 25+ min and dies at 16 GB** after adding terrain aprons | The packed grid key (`x + X·y + plane·z`) **aliases out-of-bounds coordinates**: `key(-1, y, z)` is the far cell of the previous row. Aprons touched the grid edge for the first time, the walkability BFS stepped onto a phantom "solid" at x=−1 and marched into negative space until the OOM killer fired | Bounds-check `standable()` in every pass that walks the grid, and clamp all synthetic placement (aprons) to grid bounds. Any packed-key voxel code has this trap — nothing before the aprons ever reached the edge, so it lurked through every earlier pass |
| S14 | No site, dark interiors, holes over plenums (the last "known residuals") | The IFC contains no terrain and no light fixtures; plenum roof holes can't take a solid fill | Three passes: `stamp_terrain_aprons` (grass/dirt cones sloping from every void-side ground door down to the export ground plane — also grounds the annex wings), `light_ceilings` (sea lanterns recessed into ceilings on a 6 m grid, ~930 per build), and the S12 glass covers. All never overwrite existing cells |
| S15 | **A pass that adds roof puts roof over the terrace** — 5,330 columns capped in the faithful build, 6,660 in the rectified one, each with a ceiling lantern recessed into the underside; the "interior" cell count inflated to match | `cap_envelope` decided *inside* by counting how many of a column's 8 neighbours were taller. Stand on a low roof one block from a tall wing and three of them are the wing, so the rule fires along the foot of every tall mass. The same rule in `inspect_interior` then reported those cells as holes in the envelope, and the first-person views of the "holes" showed the campus and the horizon | Decide it by **escape** instead: flood horizontally at the cell's own level through columns nothing covers, and cap only what cannot reach the edge of the model. A light well cannot escape; a terrace beside a tower can. Capping drops to 887 / 1,513. Both shapes are fixtures in `ifc_to_voxels.py --self-test` |
| S16 | **"22 stairwells bend", "28,426 cells poke outside", "2–3 wells ISOLATED but nothing is behind them"** — three numbers that sounded like findings and were not | Each counted a population without locating it. Two straight flights that touch share a well and read as a bend; 28,426 cells is a symptom with no fix attached; and nobody had walked to an ISOLATED well | Report **where**: facings level by level (21 wells actually turn as they rise, 3 hold two facings on one landing), crossings clustered into 191 openings with a representative single-step edge each, and a first-person frame at the cell named. The ISOLATED flight at (179, 228) turned out to be an exterior stair hanging in the air with no landing at either end |
| S17 | **"I see a lot of gaps where I'm not supposed to see any"** — a person looking at the rendered frames, when every number in the report said the build was fine | `--rectify` assigns an element to a wing by its CENTROID. Right for a wall; wrong for a floor slab, which spans the wing and the spine, so its centroid is outside the hull. Per hull, **90-99% of walls rotated against 25-78% of plates**: the wing's walls swung 32 or 58 degrees away and their floor stayed. Wall voxels -3.4%, glass -5%, floor unchanged. 10.7% of interior stand cells could see straight out, against 1.6% faithful | A rigid motion applied to a REGION has to cut what crosses the region's boundary. `apply_wings_piecewise` subdivides a disagreeing mesh below 0.5 m and assigns **per triangle**; aggregates still move whole. See-through 10.7% -> 3.2%, envelope holes 1,828 -> 390, and the stairwell that was ISOLATED because its landings stayed behind is connected. NB three hypotheses were measured first and two were wrong: straddling elements (the worst wing has the fewest), overlapping hulls (0 m²), and diagonal 8-connected wall leaks (13 cells of 4,151) |
| S18 | A metric that is itself the bug: **`see_through` did not exist**, so nothing in the report could have caught S17 | Reachability, holes-to-sky and crossings all measure the roof and the floor. None of them asks the one question a person asks in the first second: can I see out sideways from in here | Cast the four axis rays per interior stand cell (four cumulative sums per level, not a ray per cell) and cluster what escapes. Also `render(..., diagnose=True)`, which says whether a blank pixel LEFT THE GRID, ran out of MAX_STEPS, or is just a wall 0.6 m from the camera — the flat grey frames that started this were the third |
| S19 | **A pass that closes holes closes doorways too.** Walling every indoors/outdoors boundary near a wing seam wrote 9,842 cells, stranded 2,500 interior cells and **sealed 326 of the building's 2,216 entrances** -- to close 96 more leaks than the narrow rule | The rule could not tell a torn seam (the FLOOR stops, and there is a canyon beyond it) from a facade whose roof stops while the floor runs on -- which is where doors, colonnades and entrance canopies live. Guarding it against glazed columns and door columns halved the damage and did not remove it | Wall only where the floor ITSELF stops: an exposed plate edge, headroom above it, a ceiling not far overhead, inside the seam band. 1,147 cells, 276 leaks closed, 414 of 418 genuine openings kept, reachable share unmoved. Measure a wall-adding pass by what it SEALS (entrances, reachable cells, cut-off cells), never by the metric it improves |
| S20 | **`--rectify` leaves the glazing behind.** Audited floor by floor against the recovered model's own architectural plan, the wing moves leave 485 broken joins and 120 clashes over ten levels, and **409 of those 605 findings (68%) are Curtain Wall Mullions and Curtain Panels** -- at the same plan position on storey after storey | `wall_plan` builds every wing hull from `IfcWall` + `IfcWallStandardCase` placements. A curtain wall is neither, so it never enters the hull: the wall behind the glazing rotates 32 degrees and the glazing stays where it was, driven through the rooms that moved. This is also the unexplained 5% drop in glass voxels under `--rectify` that the per-triangle fix (S17) recovered none of -- there was nothing in the hull to cut | `adjacency_claims`: an element the hull missed travels with the wing when it TOUCHES something the wing claimed, bounded twice -- contact, and ALL of the element within reach of the hull (the box's FARTHEST corner, or a 40 m corridor that reaches a wing at one end is dragged whole into it: 138 see-through cells on a fixture that had none). Reachable interior 90.1% -> **95.3%**, beating the faithful build for the first time; largest stranded pocket 1,376 -> 293 cells. The boundary MOVES rather than vanishes -- findings within 2 m of the hull go 493 -> 7 and those at 5-10 m go 16 -> 357, which is the reach itself. The model has no wing structure to use instead (one IfcBuilding, no zones, no assemblies), so a wing is inferred and an inferred boundary breaks joins somewhere. NB the audit only exists because the plan was drawn in a renderer that draws buildings -- the stick diagram of wall footprints could not show a wall crossing a room, because it did not draw rooms |

### General

- **Cell contests need domain priorities.** `CLASS_PRIORITY` is not aesthetic:
  glass < railing < frame < roof < floor < structure < wall < stair. Anything
  the player walks on/through must win.
- **Stored block states are forever.** Saved worlds, `.schem`/`.litematic`
  pastes and prismarine renderers all use the state you wrote — fence arms,
  stair shape, door halves/hinges must be computed at export time.
- **Statistics can share the bug's blind spot.** The door-hoisting bug passed
  its own metric because the metric used the same "highest surface" logic.
  The in-client sweep caught it; after that, `verify_blocks.py` gained the
  free-standing-door check the bug would have tripped.

---

## The verification workflow (use this after any pipeline change)

```sh
# 1. data-level QA over every door/fence/stair
.venv/bin/python scripts/verify_blocks.py out/unbc_1m

# 2. export + load the real renderer's own loader (round-trip check)
renderers/mcweb/run.sh export out/unbc_1m/blocks.csv /tmp/world
node renderers/mcweb/verify_save.js /tmp/world

# 3. walk it in the actual client (relay/local build), teleport to hotspots
#    press T →  /tp <x> <y> <z>      (first /tp per session is reliable)
#    add &setting=enableLighting:false to the URL for fullbright interiors
```

`out/<name>/doors.csv` maps every door's IfcDoor **GlobalId → placed
x/y/z/facing/leaves/sill-offset**. Any door you dislike in game can be pinned
without code changes via `--overrides`:

```json
{"doors": {"2DIGqAO$r3AOgmfxj9FtOc": {"raise": 1, "facing": "north", "leaves": 2},
           "0BTBFw6f90Nfh9rP1dlXr2": {"skip": true}}}
```

---

## What the player does (deployed page behaviour)

On the GitHub Pages site (root = minecraft-web-client):

- **Boot**: no menu — the page redirects itself to `?map=world.zip` and the
  in-browser singleplayer server (flying-squid running in the tab; no backend
  anywhere) loads the UNBC world. The FPS/debug reader is disabled via
  `setting=renderDebug:"none"`.
- **Spawn**: on/above the central roof deck; you fall gently onto it.
- **Move**: WASD + mouse (click to capture), space jumps, double-space toggles
  fly in creative. `T` opens chat: `/tp x y z` works.
- **Doors are real**: right-click opens/closes both halves; double doors are
  hinge-mirrored and meet in the middle.
- **Stairs are climbable**: walk up oriented stair blocks (no jumping); the
  spiral staircase is a synthesized newel-and-tread spiral.
- **Railings**: one-block fences with connected rails; they still block falls
  (fences collide 1.5 blocks tall).
- **Read-only world**: the top-left `file-off` icon means edits are not saved
  back — every visitor gets the pristine building. Block breaking works in
  your session only.
- **Interiors are dark** (real lighting, no light sources in the model): use
  `&setting=enableLighting:false` for inspection, or see "open items".
- `/blockcraft/` is the lightweight cube-engine fallback: same building, flat
  world, in-browser generation, right-click flat doors.

## What we might still need to do

Ranked roughly by value:

1. **Interior lighting** — the model has no light sources, so interiors are
   dark with vanilla lighting. Options: emit light blocks (e.g. sea lanterns /
   light blocks) under `IfcLightFixture`s or on ceilings every N cells; or
   document the `enableLighting:false` URL for visitors.
2. **0.5 m pitch deploy** — doors/stairs/railings all scale (`make p05`), and
   fine pitch fixes most remaining "diagonal facade" artifacts; needs a
   heavier world zip (~1M blocks) and a slower first load.
3. **The last oddballs** — 92 free-standing glazed-facade doors (nothing to
   bridge to at 1 m), 2 genuine split-level pairs, 1 floating door. All are
   pin-able today via `--overrides` + `doors.csv`; a curated override file for
   the UNBC model would zero them out.
4. **BlockCraft floating corners** — the campus sits on a slope; parts of the
   building hover above BlockCraft's flat world. Stamp terrain under the
   footprint (raise ground per column to the building's lowest solid).
5. **Pin the minecraft-web-client release** — the Pages workflow tracks
   `releases/latest`; pin a tag for reproducible deploys once happy.
6. **Sounds** — background music streams from an external CDN; absent/blocked
   environments just get silence (cosmetic).
7. **Elevators/shafts** — `IfcTransportElement` is currently unmapped
   ("other"); could become open shafts with ladders or synthesized stairs.
