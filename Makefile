# RVT/IFC -> Minecraft voxel pipeline.
# Override the source file or resolution on the command line, e.g.:
#   make p1 IFC="Some Other Model.ifc"
#   make rvt RVT="Some Other Model.rvt"
#   make voxels PITCH=0.25 NAME=unbc_25cm

IFC   ?= UNBC Model - 2026-06-30 - FINAL (Fixed Library).ifc
RVT   ?= UNBC Model - 2026-06-30 - FINAL (Fixed Library) (1).rvt
PITCH ?= 1.0
NAME  ?= unbc_$(subst .,p,$(PITCH))m
PY    := .venv/bin/python
# Which pipeline output the renderer dev servers load (run 'make p1' first).
WORLD ?= out/unbc_1m
MCWEB_WORLD ?= $(WORLD)

.PHONY: help setup parser-setup parser-check contract rectify-preview \
        fixture fixture-recovery walk inspect confirm selftest p1 p05 all voxels rvt \
        viewer mcweb blockcraft blockcraft-static blockcraft-stop clean

help:
	@echo "Pipeline:"
	@echo "  make setup             create .venv and install dependencies"
	@echo "  make p1                full pipeline at 1.0 m  -> out/unbc_1m + viewer data"
	@echo "  make p05               full pipeline at 0.5 m  -> out/unbc_0p5m + viewer data"
	@echo "  make all               run both p1 and p05"
	@echo "  make voxels            full pipeline at PITCH=$(PITCH) (NAME=$(NAME))"
	@echo ""
	@echo "Parser (RVT -> IFC without Revit, via the parsers/reviter submodule):"
	@echo "  make parser-setup      check out the submodule and install its deps"
	@echo "  make parser-check      preflight the parser (needs no model)"
	@echo "  make rvt               RVT=$(RVT) straight through to a world"
	@echo "  make contract          grade IFC=... against what the engine reads"
	@echo "  make rectify-preview   see what --rectify would do to IFC=..., in seconds"
	@echo ""
	@echo "Checking a build by walking it (no model, no client, no network):"
	@echo "  make fixture           build a small IFC test building with an off-grid wing"
	@echo "  make fixture-recovery  the same building shaped as an RVT recovery, both walked"
	@echo "  make walk              first-person walkthrough of WORLD=$(WORLD)"
	@echo "  make inspect           sweep the interior: cut-off pockets, leaks, stairs"
	@echo "  make confirm           first-person frames for what inspect claims"
	@echo "  make selftest          every engine self-test, no model needed"
	@echo ""
	@echo "Renderer dev servers (all load WORLD=$(WORLD); override with WORLD=out/unbc_0p5m):"
	@echo "  make viewer            Three.js inspection viewer      http://127.0.0.1:8765/"
	@echo "  make mcweb             minecraft-web-client (real vanilla blocks: doors,"
	@echo "                         stairs, fences)                 http://localhost:3000/"
	@echo "  make blockcraft        BlockCraft, multiplayer dev (server :3002 + client)"
	@echo "                                                         http://localhost:3001/"
	@echo "  make blockcraft-static BlockCraft, serverless static build (what GitHub"
	@echo "                         Pages serves)                   http://localhost:3003/"
	@echo "  make blockcraft-stop   stop the BlockCraft dev server + client"
	@echo ""
	@echo "  make clean             remove generated out/ and web/data/ trees"

setup:
	/opt/homebrew/bin/python3.11 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-pipeline.txt pillow

# The parser is a Node/TypeScript project pinned as a submodule; the seam
# between it and this engine is the IFC contract, not code (see REVITER.md).
parser-setup:
	git submodule update --init parsers/reviter
	npm ci --prefix parsers/reviter

# Deliberately the system python, not $(PY): the preflight needs nothing but
# node, so it must be answerable before `make setup` has built the venv --
# which is exactly when someone is trying to find out why the parser will not
# run. `contract` and `rvt` do need the venv (ifcopenshell).
parser-check:
	python3 scripts/rvt_to_ifc.py --check

contract:
	$(PY) scripts/check_ifc_contract.py "$(IFC)"

# What --rectify would do, from wall placements alone: seconds, not the ~40
# minutes a full conversion takes to answer the same question.
rectify-preview:
	$(PY) scripts/preview_rectify.py "$(IFC)" \
	  --svg out/rectify-preview.svg --json out/rectify-preview.json

# A complete little building -- slabs, walls, openings, doors, an off-grid wing
# -- that the whole pipeline runs on in seconds. The UNBC model is in neither
# repository; this is what can be regression-tested without it.
fixture:
	$(PY) scripts/make_fixture_building.py --out out/fixture/building.ifc \
	  --storeys 2 --shared-placement
	$(PY) scripts/pipeline.py out/fixture/building.ifc --pitch 1.0 \
	  --name fixture --no-web --no-preview

# The same building written the way an RVT recovery writes it -- one shared
# placement, tessellated bodies -- through the same pipeline. If these two
# disagree, the RVT path is broken and no percentage on the Revit-exported
# file would have said so.
fixture-recovery: fixture
	$(PY) scripts/ifc_to_voxels.py out/fixture/building-recovery.ifc --pitch 1.0 \
	  --rectify --out-dir out/fixture-recovery
	@echo "--- Revit-shaped ---"      && $(PY) scripts/audit_walkability.py out/fixture/blocks.csv | head -1
	@echo "--- recovery-shaped ---"   && $(PY) scripts/audit_walkability.py out/fixture-recovery/blocks.csv | head -1

# Walk the world and render what a player sees. A percentage cannot show a
# corridor that pinches shut; this can.
walk:
	$(PY) scripts/walk_voxels.py "$(WORLD)/blocks.csv" --out "$(WORLD)/walk"
	$(PY) scripts/audit_walkability.py "$(WORLD)/blocks.csv"

# Where the cut-off cells are, where the envelope leaks, whether the stairs
# bend -- and a view from every region rather than one route.
inspect:
	$(PY) scripts/inspect_interior.py "$(WORLD)/blocks.csv" --out "$(WORLD)/inspect" --views 12

# The same sweep, but rendering the CLAIMS: climb the stairwells that turn,
# stand on the roof the player can reach, look up out of the holes, and look
# out through the openings they escape by. Numbers have been wrong here in
# ways only a frame showed.
confirm:
	$(PY) scripts/inspect_interior.py "$(WORLD)/blocks.csv" --out "$(WORLD)/confirm" \
	  --views 4 --stair-views 6 --outside-views 4

# Everything that can be checked without a model. Run before any push.
selftest:
	$(PY) scripts/ifc_to_voxels.py --self-test
	$(PY) scripts/inspect_interior.py --self-test
	$(PY) scripts/walk_voxels.py --self-test
	$(PY) scripts/preview_rectify.py --self-test
	$(PY) scripts/check_ifc_contract.py --self-test
	$(PY) scripts/rvt_to_ifc.py --self-test

# Stage 0 + the whole pipeline, from the proprietary format.
rvt:
	$(PY) scripts/pipeline.py "$(RVT)" --pitch $(PITCH) --name $(NAME)

p1:
	$(PY) scripts/pipeline.py "$(IFC)" --pitch 1.0 --name unbc_1m

p05:
	$(PY) scripts/pipeline.py "$(IFC)" --pitch 0.5 --name unbc_0p5m

all: p1 p05

voxels:
	$(PY) scripts/pipeline.py "$(IFC)" --pitch $(PITCH) --name $(NAME)

viewer:
	scripts/serve_viewer.sh

# Export MCWEB_WORLD's blocks.csv to an Anvil save and launch the local
# minecraft-web-client (real vanilla block models: doors/stairs/slabs/fences).
# serve_client.sh clones + wires up the client and auto-loads the world.
mcweb:
	renderers/mcweb/run.sh export "$(MCWEB_WORLD)/blocks.csv" "$(MCWEB_WORLD)/world"
	renderers/mcweb/run.sh pack "$(MCWEB_WORLD)/world"
	renderers/mcweb/serve_client.sh "$(MCWEB_WORLD)/world.zip"

# BlockCraft (cube-engine fallback renderer). Both targets regenerate the
# building data from WORLD's blocks.csv first (idempotent, ~seconds).
blockcraft:
	$(PY) scripts/setup_blockcraft.py "$(WORLD)/blocks.csv"
	scripts/run_blockcraft.sh

blockcraft-static:
	$(PY) scripts/setup_blockcraft.py "$(WORLD)/blocks.csv"
	scripts/build_blockcraft_static.sh serve

blockcraft-stop:
	scripts/run_blockcraft.sh stop

clean:
	rm -rf out/unbc_* web/data/unbc_* web/data/datasets.json
