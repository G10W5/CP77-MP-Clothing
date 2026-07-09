# CPMP Handoff — Two-Sided Normal Bug + File-Count Architecture Question

**Project:** CP77 Modding Pipeline Master (CPMP) v4.7
**Stack:** Python, PySide6 GUI (`cpmp_qt.py`), tkinter legacy GUI (`cpmp.py`), WolvenKit CLI
**Files involved:** `cr2w_mesh.py`, `cpmp.py`
**Recovery note:** repo was restored from a commit ~2 days old after an accidental `shutil.rmtree` wipe. Diff everything you touch against what's currently on disk before assuming it matches what's described below — some recent work (per-color/per-foot-state entity generation in `compile_json_templates`) may or may not be present depending on what the restored commit had.

---

## Task 1 (priority — do this first): Fix two-sided material normal/tangent flip

**Symptom:** two-sided cloth meshes (e.g. capes, trains, fishnet-style geometry) render with a "shattered glass" glitch/flicker — chaotic specular highlights, incoherent shading on the backface duplicate.

**Root cause:** in `cr2w_mesh.py`, `import_glb_to_mesh()`, the backface-duplicate normal/tangent flip only negates one axis component instead of the full vector.

**Location:** `cr2w_mesh.py`, inside the two-sided geometry doubling block (~line 649–674).

**Current (buggy) code:**
```python
if dup['normals'] is not None:
    dup['normals'] = dup['normals'].copy()
    dup['normals'][:, 1] *= -1
if dup['tangents'] is not None:
    dup['tangents'] = dup['tangents'].copy()
    dup['tangents'][:, 1] *= -1
```

**Why it's wrong:** `transform_normals()` (line ~158) later remaps glTF axes to REDengine axes as `(X, -Z, Y)`. Flipping only the source glTF Y component ends up flipping only *one* axis of the final in-engine normal — not a true mirror/opposite normal. Same axis-swap convention is used again at line ~474 for the main (non-doubled) tangent packing: `tx, ty, tz = t[0], -t[2], t[1]` — so any fix here should stay consistent with that mapping.

**Fix — negate the full vector, not one column:**
```python
if dup['normals'] is not None:
    dup['normals'] = -dup['normals'].copy()
if dup['tangents'] is not None:
    dup['tangents'] = dup['tangents'].copy()
    dup['tangents'][:, :3] = -dup['tangents'][:, :3]   # negate xyz, leave w (index 3, handedness) untouched for now
```

**Test plan:**
1. Apply the normal-only fix first, rebuild a two-sided test mesh, check in-game/in WolvenKit's mesh preview for the shattered/glitchy look going away.
2. If normal-mapped detail (bump highlights) still looks wrong on the backface specifically, revisit whether `tangents[:, 3]` (the `w` handedness component) also needs to flip — since the face winding is already being reversed a few lines above (`idx[i], idx[i+1] = idx[i+1], idx[i]`), reversing winding changes chirality, which *may* also require flipping `w` to keep tangent-space lighting consistent. Don't assume this in advance — verify visually.
3. Confirm the fix doesn't break single-sided meshes (this code path is gated behind `if two_sided_materials:`, so it shouldn't, but confirm no regression on a non-two-sided item).

---

## Task 2 (architecture discussion, not yet approved for implementation)

**Symptom under investigation:** `compile_json_templates()` in `cpmp.py` generates a large number of files per item — one root `.ent` + one appearance `.app` per color variant, plus one mesh `.ent` per foot state. For a 5-color, 3-foot-state item this produces ~16+ files (matches what's visible in the WolvenKit project tree).

**Finding:** This is not a duplication bug — `orchestrate_pipeline()` only calls `compile_json_templates()` once per build. The file count is inherent to the "vanilla item-addition" style architecture currently implemented (one entry per color × foot-state suffix).

**Relevant official documentation:** the redmodding wiki's ArchiveXL "Dynamic Variants" page describes this exact scaling problem and the fix the community adopted — moving the color/foot-state resolution logic into the mesh entity file itself via conditional component loading, instead of stamping out a separate root entity / appearance file per suffix combination. Reported savings in the wiki's own example: dropping from ~120 root-entity entries + 120 app entries + 6 mesh_entity files down to a single dynamically-resolved setup.

**I (the user) have downloaded the official wiki's tutorial project for this** — it should be provided alongside this handoff, or ask me for it before starting this task. Use it as the reference implementation for correct `.xl` file structure, conditional component syntax, and root entity `DynamicAppearance`/dynamic variant tagging — don't guess at the schema from the wiki prose alone.

**Do NOT implement this refactor yet.** This is a bigger architecture change to `compile_json_templates()` (and possibly `sync_textures`/`generate_configs`) than the normal-flip bug fix, and should be scoped as its own task after Task 1 is verified working. Flag back with:
- A short comparison of current per-suffix generation vs. what the tutorial project's dynamic variant setup looks like structurally
- An estimate of what changes to `compile_json_templates()` would be needed
- Any open questions about how CPMP's existing per-item foot-state / color-variant UI data would map onto dynamic variant conditionals

---

## Notes for the agent

- Don't touch GUI files (`cpmp_qt.py`, the tkinter UI code in `cpmp.py`) unless a task explicitly calls for it — these were just migrated from tkinter to PySide6 and are in a stable state.
- All mesh templates must be sourced from vanilla CDPR assets only (redistribution constraint) — don't substitute or reference third-party mod meshes for anything.
- Root-cause diagnosis before fixes — if something looks off beyond what's described here, report it rather than silently patching around it.
