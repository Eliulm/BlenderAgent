# BlenderAgent

One researched physical object becomes a labelled Blender scene and separate
world-coordinate OBJ meshes for EM meshing. Research runs on GPT-6 Luna; the
reference reconstruction loop runs on GPT-6 Astra. The builder is deliberately
three files: `builder/prompt.txt`, `builder/verify.py`, and `builder/runner.py`.

```sh
cd /Users/elwa/repos/building/BlenderAgent
python3 run.py --product 'Exact manufacturer and model' --name unique_asset
```

For existing evidence or your own images, create an `evidence.json` in the
researcher format and run:

```sh
python3 run.py --evidence /absolute/path/evidence.json --name unique_asset
```

The researcher selects up to ten downloaded reference images, with descriptions
and source pages, a required part list, whole-product dimensions, and explicit
evidence gaps. It judges EM-relevant detail for the intended 20–30 GHz band.
Luna downloads the selected images itself into the research run's `images/`
directory; the runner checks the saved files before the visual audit. If a
download fails, Luna repairs the evidence and keeps useful earlier research.
Run the same command with `--resume` after an interrupted research step.
Simple exposed objects need fewer views and usually no teardown.
Enclosed electronics generally need interior evidence when it exists. Review or
edit `research/<name>/evidence.json` before building if source quality matters.
After downloading, Luna sees the actual images once to correct viewpoint labels,
exclude detachable accessories, and check part and dimension claims.

For manually supplied references, image paths in `evidence.json` are relative to
that file. A minimal record is:

```json
{
  "product": "Manufacturer and exact model",
  "images": [
    {"path": "images/front.jpg", "kind": "exterior",
     "description": "Front view, assembled, controls visible", "source_url": "https://example.com/product"}
  ],
  "parts": [
    {"name": "Outer housing", "material": "polymer", "location": "around the body",
     "reason": "exterior material boundary", "basis": "observed", "evidence": ["image_01"]}
  ],
  "dimensions": [], "gaps": [], "notes": []
}
```

Add interior images and required internal parts for enclosed assemblies. If
whole-product measurements are available, use `dimensions` entries with
`label`, `value_mm`, and `source_url`; the builder maps those to XYZ bounds.
For weapon-like references, the builder prompt requests only an inert EM
surrogate: coarse inferred internal material regions and blind exterior
recesses, without operating mechanisms. Such an asset does not represent the
true internal EM response.

Each Astra response contains only a complete Blender recipe, exact code edits,
or an acceptance signal, plus an `accept_if_verified` choice. Astra thinks
through verifier errors and visual discrepancies internally, then returns a
single integrated code edit without a written review. The runner applies code
and waits for the isolated Blender verifier; no model tool calls or polling
are part of this loop. The verifier checks closed labelled solids, part
overlap and declared overall scale.
It renders a multi-angle sheet, including interior context and cutaways when
interior parts are tagged. The model gets that sheet and the compact result,
then decides whether to repair, refine, or accept. Follow-ups reuse the prior
thread without resending the full recipe unless an edit fails. From the first
render, Astra separately diagnoses verifier errors and major visual shape
differences, then combines their fixes in one edit. A passing edited candidate
with `accept_if_verified=true` is published without
another model turn; the first build always receives visual review. This saves
an acceptance-only turn but does not include model inspection of the final
edit's new render. Use `--keep-work` to retain that preview for audit. There is
no fixed refinement count. `--effort medium` or `high` can be used for harder
objects.
The contact sheet defaults to 1440 pixels wide, with tighter framing so each
object is larger without making a many-view sheet excessively tall. The
original research images
are retained, while copies attached to Luna for visual audit and to Astra for
building are downscaled to a 1260-pixel maximum side. Use
`--preview-width 2100` or `--reference-max-side 2100` when small details need
more resolution.

On success, `output/<name>/` contains only `asset.blend` and `parts/*.obj`.
OBJ filenames include role and material family, for example
`left_hinge__steel.obj`; vertex coordinates are in metres. The .blend retains
part tags, assembly names, scale basis, and assumptions. It is a geometry
handoff for gprMAX preparation; the material names are not numerical EM
properties. Stock gprMax's documented mesh path voxelizes supported meshes to
`geometry.h5` with a material database; the requested OBJ files are an upstream
geometry handoff, not directly runnable simulation input. See the
[gprMax mesh import guide](https://docs.gprmax.com/en/latest/inc_GeometryImport.html).
Rejected attempts and logs stay in a hidden `.work/` directory
only when a run fails or `--keep-work` is given. Research images and evidence
stay in `research/<name>/` for provenance and reuse.
If a process stops after a completed model response or during Blender work,
rerun the same command with `--resume`. It reuses the recorded thread and
finished model responses rather than starting a new paid builder conversation.
An incomplete in-flight model response is reported for inspection rather than
silently issuing a duplicate call.
Both runners print fresh input, cached input, and output token totals; the
researcher also prints its web-search call count. This makes turn and cache
costs observable without adding files to the published asset directory.

Prerequisites: Codex CLI with the two models, network access for Luna's
workspace-write research session,
Python 3, and Blender 5.2 (set `BLENDER_BIN` for a different install). The
builder uses the executable path in `builder/verify.py` and waits up to ten
minutes for each Blender run. Its first prompt includes the public Blender API,
the part requirements, measurements, and all selected images; model turns do
not rediscover files or launch the verifier.
