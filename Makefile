# IFC -> Minecraft voxel pipeline.
# Override the source file or resolution on the command line, e.g.:
#   make p1 IFC="Some Other Model.ifc"
#   make voxels PITCH=0.25 NAME=unbc_25cm

IFC   ?= UNBC Model - 2026-06-30 - FINAL (Fixed Library).ifc
PITCH ?= 1.0
NAME  ?= unbc_$(subst .,p,$(PITCH))m
PY    := .venv/bin/python
# Which pipeline output the renderer dev servers load (run 'make p1' first).
WORLD ?= out/unbc_1m
MCWEB_WORLD ?= $(WORLD)

.PHONY: help setup p1 p05 all voxels viewer mcweb blockcraft blockcraft-static blockcraft-stop clean

help:
	@echo "Pipeline:"
	@echo "  make setup             create .venv and install dependencies"
	@echo "  make p1                full pipeline at 1.0 m  -> out/unbc_1m + viewer data"
	@echo "  make p05               full pipeline at 0.5 m  -> out/unbc_0p5m + viewer data"
	@echo "  make all               run both p1 and p05"
	@echo "  make voxels            full pipeline at PITCH=$(PITCH) (NAME=$(NAME))"
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
