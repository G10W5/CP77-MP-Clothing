#!/usr/bin/env python3
"""
CR2W Mesh Converter
====================
Converts Blender GLB files into Cyberpunk 2077 CR2W .mesh format.

Uses pygltflib for GLB parsing and WolvenKit CLI for CR2W serialization.
"""

import base64
import copy
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Set, Tuple

import numpy as np
import pygltflib


def _log(msg: str, logger=None, level: str = "info"):
    """Log a message using the provided logger or print."""
    if logger:
        logger(msg, level)
    else:
        print(msg)


def float_to_half(f: float) -> int:
    """Convert float32 to float16 (half-precision)."""
    return struct.unpack('H', struct.pack('e', f))[0]


def pack_position(x: float, y: float, z: float,
                  qscale: dict, qoffset: dict) -> bytes:
    """Pack position to Short4N format (4 x int16).
    Input x,y,z are already in REDengine coordinates."""

    def _quantize(val, scale, offset):
        v = (val - offset) / scale if scale != 0 else 0.0
        v = max(-1.0, min(1.0, v))
        return int(v * 32767)

    qx = _quantize(x, qscale['X'], qoffset['X'])
    qy = _quantize(y, qscale['Y'], qoffset['Y'])
    qz = _quantize(z, qscale['Z'], qoffset['Z'])
    qw = 32767

    return struct.pack('<hhhh', qx, qy, qz, qw)


def pack_normal(nx: float, ny: float, nz: float) -> int:
    """Pack normal to Dec4 uint32.
    Bit layout: X(10) | Y(10) | Z(10) | W(2, unused=0)
    Quantization: (v+1)*511.5 maps [-1,1] to [0,1023]."""
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length > 0:
        nx /= length
        ny /= length
        nz /= length

    def _quantize(v):
        v = max(-1.0, min(1.0, v))
        return int(round((v + 1.0) * 511.5)) & 0x3FF

    ix = _quantize(nx)
    iy = _quantize(ny)
    iz = _quantize(nz)

    return ix | (iy << 10) | (iz << 20)


def pack_tangent(nx: float, ny: float, nz: float, w: float) -> int:
    """Pack tangent to Dec4 uint32.
    Bit layout: X(10) | Y(10) | Z(10) | W(2)
    W=+1 -> bits 30-31 = 0b00, W=-1 -> bits 30-31 = 0b11
    Quantization: (v+1)*511.5 maps [-1,1] to [0,1023]."""
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length > 0:
        nx /= length
        ny /= length
        nz /= length

    def _quantize(v):
        v = max(-1.0, min(1.0, v))
        return int(round((v + 1.0) * 511.5)) & 0x3FF

    ix = _quantize(nx)
    iy = _quantize(ny)
    iz = _quantize(nz)

    w_bits = 0 if w >= 0 else 3
    return ix | (iy << 10) | (iz << 20) | (w_bits << 30)


def pack_uv(u: float, v: float) -> bytes:
    """Pack UV to Float16_2 format."""
    return struct.pack('<HH', float_to_half(u), float_to_half(v))


def pack_color(r: float, g: float, b: float, a: float = 1.0) -> bytes:
    """Pack color to 4 bytes (RGBA)."""
    r_byte = max(0, min(255, int(round(r * 255))))
    g_byte = max(0, min(255, int(round(g * 255))))
    b_byte = max(0, min(255, int(round(b * 255))))
    a_byte = max(0, min(255, int(round(a * 255))))
    return struct.pack('BBBB', r_byte, g_byte, b_byte, a_byte)


def compute_quantization(positions: np.ndarray) -> Tuple[dict, dict]:
    """Compute quantization scale and offset from positions array."""
    min_pos = positions.min(axis=0)
    max_pos = positions.max(axis=0)

    qoffset = {
        'X': float(min_pos[0]),
        'Y': float(min_pos[1]),
        'Z': float(min_pos[2])
    }

    range_x = float(max_pos[0] - min_pos[0])
    range_y = float(max_pos[1] - min_pos[1])
    range_z = float(max_pos[2] - min_pos[2])

    qscale = {
        'X': range_x if range_x > 0 else 1.0,
        'Y': range_y if range_y > 0 else 1.0,
        'Z': range_z if range_z > 0 else 1.0
    }

    return qscale, qoffset


def flip_face_winding(indices: np.ndarray) -> np.ndarray:
    """Convert GLTF CCW winding to REDengine CW winding.
    WolvenKit swaps indices 0 and 1 per triangle."""
    if indices.ndim == 1:
        indices = indices.reshape(-1, 3)
    result = indices.copy()
    result[:, 0], result[:, 1] = result[:, 1].copy(), result[:, 0].copy()
    return result


def transform_coord_sys(positions: np.ndarray) -> np.ndarray:
    """Transform from GLTF (right-handed Y-up) to REDengine (left-handed Z-up)."""
    result = np.zeros_like(positions)
    result[:, 0] = positions[:, 0]
    result[:, 1] = -positions[:, 2]
    result[:, 2] = positions[:, 1]
    return result


def transform_normals(normals: np.ndarray) -> np.ndarray:
    """Transform normal vectors from GLTF to REDengine coordinate system."""
    result = np.zeros_like(normals)
    result[:, 0] = normals[:, 0]
    result[:, 1] = -normals[:, 2]
    result[:, 2] = normals[:, 1]

    lengths = np.linalg.norm(result, axis=1, keepdims=True)
    lengths[lengths == 0] = 1.0
    result /= lengths

    return result


def get_glb_material_names(glb_path: str) -> list:
    """Return per-primitive material names from a GLB file.

    Uses the same logic as extract_glb_data() so GUI material settings
    (transparent, two-sided) correctly match the converter's material names.
    Lightweight — only parses headers, not vertex data.
    """
    try:
        gltf = pygltflib.GLTF2().load(glb_path)
        names = []
        for mesh in gltf.meshes:
            for pi, primitive in enumerate(mesh.primitives):
                mat_name = None
                if primitive.material is not None and primitive.material >= 0 and gltf.materials:
                    mat = gltf.materials[primitive.material]
                    mat_name = mat.name or f"material_{primitive.material}"
                if not mat_name:
                    mat_name = mesh.name or f"mesh_{gltf.meshes.index(mesh)}_prim_{pi}"
                names.append(mat_name)
        return names
    except Exception:
        return []


def extract_glb_data(glb_path: str, apply_garment_support: bool = False, disable_garment_support: bool = False) -> list:
    """Extract vertex data from GLB file.

    Returns a list of per-primitive dicts, one per GLB mesh primitive.
    Each dict contains positions, normals, tangents, uvs, colors, joints,
    weights, indices, has_bones, material_name, and vertex_count.

    Multi-primitive GLBs produce multiple entries (one per material group).
    Single-primitive GLBs produce a single entry (backward compatible).

    When apply_garment_support is False, the GarmentSupport morph target in the
    GLB is ignored — the caller will get a zero-filled morphOffsets buffer
    instead. This prevents the game from auto-shrinking the garment under
    other items when the modder did not author the deformation.
    """
    gltf = pygltflib.GLTF2().load(glb_path)
    raw_blob = gltf.binary_blob()

    def get_accessor_data(accessor_idx):
        if accessor_idx is None:
            return None
        accessor = gltf.accessors[accessor_idx]
        buffer_view = gltf.bufferViews[accessor.bufferView]

        data_offset = buffer_view.byteOffset + accessor.byteOffset
        count = accessor.count
        comp_type = accessor.componentType
        type_str = accessor.type

        comp_size_map = {
            5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4
        }
        comp_size = comp_size_map.get(comp_type, 4)

        type_count_map = {
            'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4
        }
        type_count = type_count_map.get(type_str, 1)

        element_size = comp_size * type_count
        stride = buffer_view.byteStride if buffer_view.byteStride else element_size

        result = np.zeros((count, type_count), dtype=np.float32)

        for i in range(count):
            offset = data_offset + i * stride
            for j in range(type_count):
                elem_offset = offset + j * comp_size
                if comp_type == 5126:
                    val = struct.unpack_from('<f', raw_blob, elem_offset)[0]
                elif comp_type == 5125:
                    val = struct.unpack_from('<I', raw_blob, elem_offset)[0]
                elif comp_type == 5123:
                    val = struct.unpack_from('<H', raw_blob, elem_offset)[0]
                elif comp_type == 5121:
                    val = raw_blob[elem_offset]
                else:
                    val = 0.0
                result[i, j] = val

        return result

    def get_indices_data(accessor_idx):
        if accessor_idx is None:
            return None
        accessor = gltf.accessors[accessor_idx]
        buffer_view = gltf.bufferViews[accessor.bufferView]
        data_offset = buffer_view.byteOffset + accessor.byteOffset
        count = accessor.count
        comp_type = accessor.componentType

        idx_stride = 4 if comp_type == 5125 else 2
        result = np.zeros(count, dtype=np.uint16)
        for i in range(count):
            offset = data_offset + i * idx_stride
            if comp_type == 5123:
                result[i] = struct.unpack_from('<H', raw_blob, offset)[0]
            elif comp_type == 5125:
                result[i] = struct.unpack_from('<I', raw_blob, offset)[0] & 0xFFFF

        return result

    primitives = []
    has_any_materials = len(gltf.materials) > 0 if gltf.materials else False

    for mesh in gltf.meshes:
        # --- GarmentSupport morph target detection ---
        # Check mesh.extras for targetNames array (set by Blender GLB exporter)
        garment_morph_idx = None
        extras = mesh.extras
        if extras:
            target_names = extras.get('targetNames', [])
            if target_names and 'GarmentSupport' in target_names:
                garment_morph_idx = target_names.index('GarmentSupport')

        for pi, primitive in enumerate(mesh.primitives):
            attributes = primitive.attributes

            mat_name = None
            if primitive.material is not None and primitive.material >= 0 and gltf.materials:
                mat = gltf.materials[primitive.material]
                mat_name = mat.name or f"material_{primitive.material}"
            if not mat_name:
                mat_name = mesh.name or f"mesh_{gltf.meshes.index(mesh)}_prim_{pi}"

            data = {
                'positions': None,
                'normals': None,
                'tangents': None,
                'uvs': None,
                'colors': None,
                'joints': None,
                'weights': None,
                'indices': None,
                'has_bones': False,
                'material_name': mat_name,
                'vertex_count': 0,
                'garment_morph': None,
            }

            if hasattr(attributes, 'POSITION') and attributes.POSITION is not None:
                data['positions'] = get_accessor_data(attributes.POSITION)
                data['vertex_count'] = len(data['positions'])

            if hasattr(attributes, 'NORMAL') and attributes.NORMAL is not None:
                data['normals'] = get_accessor_data(attributes.NORMAL)

            if hasattr(attributes, 'TANGENT') and attributes.TANGENT is not None:
                data['tangents'] = get_accessor_data(attributes.TANGENT)

            if hasattr(attributes, 'TEXCOORD_0') and attributes.TEXCOORD_0 is not None:
                data['uvs'] = get_accessor_data(attributes.TEXCOORD_0)

            if hasattr(attributes, 'COLOR_0') and attributes.COLOR_0 is not None:
                data['colors'] = get_accessor_data(attributes.COLOR_0)

            # --- Garment support color attributes ---
            # Per redmodding wiki, garment meshes need three color attributes:
            #   _GARMENTSUPPORTWEIGHT (Vertex > Color, flat red 1,0,0)
            #   _GARMENTSUPPORTCAP    (Vertex > Color, flat black 0,0,0)
            #   Col                     (Face Corner > Byte Color, flat black 0,0,0)
            # If the GLB doesn't have them, generate flat defaults so garment support works.
            gs_weight = None
            gs_cap = None
            gs_col = None
            # Check for custom attributes (Blender may export these as named attrs)
            attr_dict = {}
            for attr_name in dir(attributes):
                if not attr_name.startswith('_') and attr_name.isupper():
                    val = getattr(attributes, attr_name, None)
                    if val is not None:
                        attr_dict[attr_name] = val

            # Standard glTF color attributes
            for gs_name, attr_key in [
                ('_GARMENTSUPPORTWEIGHT', 'COLOR_1'),
                ('_GARMENTSUPPORTCAP', 'COLOR_2'),
                ('_GARMENTSUPPORTCOL', 'COLOR_3'),
            ]:
                if hasattr(attributes, attr_key) and getattr(attributes, attr_key) is not None:
                    if gs_name == '_GARMENTSUPPORTWEIGHT':
                        gs_weight = get_accessor_data(getattr(attributes, attr_key))
                    elif gs_name == '_GARMENTSUPPORTCAP':
                        gs_cap = get_accessor_data(getattr(attributes, attr_key))
                    elif gs_name == '_GARMENTSUPPORTCOL':
                        gs_col = get_accessor_data(getattr(attributes, attr_key))

            # Generate flat defaults if missing (unless garment support is disabled)
            nv = data['vertex_count']
            if disable_garment_support:
                gs_weight = None
                gs_cap = None
                gs_col = None
            else:
                if gs_weight is None:
                    gs_weight = np.ones((nv, 4), dtype=np.float32)
                    gs_weight[:, 1] = 0.0
                    gs_weight[:, 2] = 0.0
                    gs_weight[:, 3] = 1.0  # alpha
                if gs_cap is None:
                    gs_cap = np.zeros((nv, 4), dtype=np.float32)
                    gs_cap[:, 3] = 1.0  # alpha
                if gs_col is None:
                    gs_col = np.zeros((nv, 4), dtype=np.float32)
                    gs_col[:, 3] = 1.0  # alpha

            data['gs_weight'] = gs_weight
            data['gs_cap'] = gs_cap
            data['gs_col'] = gs_col

            if hasattr(attributes, 'JOINTS_0') and attributes.JOINTS_0 is not None:
                data['joints_0'] = get_accessor_data(attributes.JOINTS_0)
                data['has_bones'] = True
            if hasattr(attributes, 'JOINTS_1') and attributes.JOINTS_1 is not None:
                data['joints_1'] = get_accessor_data(attributes.JOINTS_1)
                data['has_bones'] = True
            if hasattr(attributes, 'WEIGHTS_0') and attributes.WEIGHTS_0 is not None:
                data['weights_0'] = get_accessor_data(attributes.WEIGHTS_0)
            if hasattr(attributes, 'WEIGHTS_1') and attributes.WEIGHTS_1 is not None:
                data['weights_1'] = get_accessor_data(attributes.WEIGHTS_1)
            # Backwards compat: keep 'joints'/'weights' pointing to first set
            data['joints'] = data.get('joints_0')
            data['weights'] = data.get('weights_0')
            # Detect total weight count (4 or 8)
            if data.get('joints_1') is not None:
                data['weight_count'] = 8
            else:
                data['weight_count'] = 4
            # Track whether the GLB actually had a GarmentSupport shape key.
            # The morph delta values are only real if this is True.
            data['garment_morph_detected'] = False
            data['garment_morph'] = None  # Will be set to zeros below if garment mesh

            if primitive.indices is not None:
                data['indices'] = get_indices_data(primitive.indices)

            # --- Extract GarmentSupport morph deltas if present ---
            if apply_garment_support and garment_morph_idx is not None and primitive.targets is not None:
                if garment_morph_idx < len(primitive.targets):
                    target = primitive.targets[garment_morph_idx]
                    if hasattr(target, 'POSITION') and target.POSITION is not None:
                        raw_morph = get_accessor_data(target.POSITION)
                        if raw_morph is not None and len(raw_morph) == data['vertex_count']:
                            # Transform from GLTF coords to REDengine: (X, -Z, Y)
                            morph = np.zeros_like(raw_morph)
                            morph[:, 0] = raw_morph[:, 0]
                            morph[:, 1] = -raw_morph[:, 2]
                            morph[:, 2] = raw_morph[:, 1]
                            data['garment_morph'] = morph
                            data['garment_morph_detected'] = True
                            _log(f"  GarmentSupport morph: {len(morph)} vertices, {len(raw_morph)} raw deltas", None, "info")

            primitives.append(data)

    if not primitives:
        _log(f"WARNING: GLB {glb_path} has no primitives!", None, "error")
        return []

    if not has_any_materials:
        names = [p['material_name'] for p in primitives]
        _log(f"NOTE: GLB has no glTF material data (this is normal for WolvenKit's Blender GLB exporter — "
             f"it does not preserve Blender material slot assignments). Using mesh/object names as the "
             f"material grouping key instead: {names}", None, "warn")
        _log(f"  IMPORTANT: rename your Blender MESH OBJECTS (not material slots) to match your texture "
             f"filename prefixes (e.g. an object named 'ruffle' pairs with ruffle_color_*.png textures), "
             f"then re-export the GLB. Renaming Blender materials alone will NOT fix this.", None, "warn")

    return primitives


def build_bone_map(glb_joints: List[str], template_bones: List[str]) -> dict:
    """Create mapping from GLB joint indices to template bone indices.

    Uses an explicit alias table for body-mod joints that don't exist in the
    vanilla Inner Torso template skeleton but have anatomically correct
    equivalents. This prevents the vertex explosion caused by falling back to
    bone 0 (Hips) for joints near the shoulders, thumbs, or neck.
    """
    bone_map = {}

    # Explicit aliases: maps a known body-mod joint name (lowercase) to the
    # name of the closest vanilla equivalent in the template skeleton.
    BONE_ALIASES = {
        # Body-mod shoulder extras -> vanilla shoulder joint
        'l_shl_1_jnt': 'l_SHL_0_JNT',
        'r_shl_1_jnt': 'r_SHL_0_JNT',
        # Body-mod thumb in-hand -> nearest in-hand finger (thumb not in template)
        'leftinthumb': 'LeftInHandIndex',
        'rightinthumb': 'RightInHandIndex',
        'leftinhandthumb': 'LeftInHandIndex',
        'rightinhandthumb': 'RightInHandIndex',
        # Neck variant
        'neck1': 'Neck',
        # Generic numbered variants -> strip the number
        'neck2': 'Neck',
        'spine4': 'Spine3',
    }

    template_bone_lower = {b.lower(): i for i, b in enumerate(template_bones)}

    for glb_idx, joint_name in enumerate(glb_joints):
        joint_lower = joint_name.lower()

        # 1. Exact match
        if joint_lower in template_bone_lower:
            bone_map[glb_idx] = template_bone_lower[joint_lower]
            continue

        # 2. Explicit alias table
        alias_target = BONE_ALIASES.get(joint_lower)
        if alias_target and alias_target.lower() in template_bone_lower:
            bone_map[glb_idx] = template_bone_lower[alias_target.lower()]
            continue

        # 3. Substring match (existing fallback)
        found = False
        for tmpl_name, tmpl_idx in template_bone_lower.items():
            if joint_lower in tmpl_name or tmpl_name in joint_lower:
                bone_map[glb_idx] = tmpl_idx
                found = True
                break

        # 4. Last resort: bone 0 (Hips) — only reached if truly unmatchable
        if not found and template_bones:
            bone_map[glb_idx] = 0

    return bone_map


def build_vertex_buffer(data: dict, bone_map: dict, qscale: dict, qoffset: dict,
                      slot_strides: list, weight_count: int = 4,
                      garment_morph_exists: bool = True,
                      disable_garment_support: bool = False) -> Tuple[bytes, dict]:
    """Build the multi-stream vertex buffer matching WolvenKit's format.

    Buffer layout (16-byte aligned between stream sections):
      [Stream 0: pos+joints+weights(+garmentMorph)]   padded to 16 bytes
      [Stream 1: UV0]                                  padded to 16 bytes
      [Stream 2: Normal+Tangent (Dec4)]                padded to 16 bytes
      [Stream 3: Color+UV1]                            padded to 16 bytes
      [Stream 4: GS_Weight (flat red)]                 padded to 16 bytes
      [Stream 5: GS_Cap (flat black)]                  padded to 16 bytes
      [Stream 6: GS_Col (flat black)]                  padded to 16 bytes

    Per-vertex Stream 0 layout:
      4x short  Position (PT_Short4N)                 8 bytes
      4x byte   Joints0 (PT_UByte4)                    4 bytes
      4x byte   Weights0 (PT_UByte4N)                  4 bytes
      4x byte   Joints1 (PT_UByte4)                    4 bytes (if 8 weights)
      4x byte   Weights1 (PT_UByte4N)                  4 bytes (if 8 weights)
      4x ushort GarmentMorph (PT_Float16_4)            8 bytes (if garmentSupport)
    Total = 8 + 4 + 4 [+ 4 + 4] [+ 8] = 16 [+ 8] [+ 8] = 24 or 32

    Returns (vertex_buffer_bytes, stride_info) where stride_info has:
      'vpStrides': bytes per vertex in stream 0
      'weightCounts': weight count (4 or 8)
      'garmentSupportExists': bool
    """
    positions = data['positions']
    normals = data['normals']
    tangents = data['tangents']
    uvs = data['uvs']
    colors = data['colors']
    joints_0 = data.get('joints_0')
    joints_1 = data.get('joints_1')
    weights_0 = data.get('weights_0')
    weights_1 = data.get('weights_1')
    garment_morph = data.get('garment_morph')  # always present (zero-filled if no morph)

    num_vertices = len(positions)
    has_bones = joints_0 is not None and weights_0 is not None and bone_map

    # Calculate stream 0 stride (per WolvenKit MeshImportTools.cs:1218-1225)
    vp_stride = (weight_count * 2) + 8  # 4*2+8=16 or 8*2+8=24
    if garment_morph_exists:
        vp_stride += 8  # 24 or 32

    # Transform coordinates (GLTF Y-up to REDengine Z-up)
    transformed_positions = transform_coord_sys(positions)
    if normals is not None:
        transformed_normals = transform_normals(normals)
    else:
        transformed_normals = None

    # ===== STREAM 0: pos + joints + weights (+ garment morph) =====
    stream0 = bytearray()
    for i in range(num_vertices):
        # Position: 3x short quantized + 1x short W=32767
        pos = transformed_positions[i]
        stream0.extend(pack_position(pos[0], pos[1], pos[2], qscale, qoffset))

        if has_bones:
            # Map all 8 joint influences
            j0_data = joints_0[i] if joints_0 is not None else np.zeros(4, dtype=np.float32)
            j1_data = joints_1[i] if joints_1 is not None else np.zeros(4, dtype=np.float32)
            w0_data = weights_0[i] if weights_0 is not None else np.zeros(4, dtype=np.float32)
            w1_data = weights_1[i] if weights_1 is not None else np.zeros(4, dtype=np.float32)

            # Combine all influences, remap to template skeleton, normalize
            all_influences = []
            for j in range(4):
                if w0_data[j] > 0 and int(j0_data[j]) in bone_map:
                    all_influences.append((bone_map[int(j0_data[j])], w0_data[j]))
            for j in range(4):
                if w1_data[j] > 0 and int(j1_data[j]) in bone_map:
                    all_influences.append((bone_map[int(j1_data[j])], w1_data[j]))

            # Sort by weight descending, take top 4
            all_influences.sort(key=lambda x: -x[1])
            top4 = all_influences[:4]

            # Normalize weights
            total = sum(w for _, w in top4) or 1.0
            normalized = [w / total for _, w in top4]
            while len(normalized) < 4:
                normalized.append(0.0)

            # Pack 4x byte joints + 4x byte weights (first set)
            mapped_j0 = [int(idx) for idx, _ in top4] + [0] * (4 - len(top4))
            mapped_w0 = [int(min(255, max(0, round(w * 255)))) for w in normalized] + [0] * (4 - len(normalized))
            stream0.extend(struct.pack('BBBB', *mapped_j0))
            stream0.extend(struct.pack('BBBB', *mapped_w0))

            # Pack 4x byte joints + 4x byte weights (second set, for 8-weight)
            if weight_count > 4:
                stream0.extend(struct.pack('BBBB', 0, 0, 0, 0))  # joints1 (no more influences)
                stream0.extend(struct.pack('BBBB', 0, 0, 0, 0))  # weights1 (all zero)
        else:
            # No bones: write zeros for joints/weights
            stream0.extend(struct.pack('BBBB', 0, 0, 0, 0))
            stream0.extend(struct.pack('BBBB', 0, 0, 0, 0))
            if weight_count > 4:
                stream0.extend(struct.pack('BBBB', 0, 0, 0, 0))
                stream0.extend(struct.pack('BBBB', 0, 0, 0, 0))

        # Garment morph: 3x ushort (half-float) + 1x ushort (zero padding)
        if garment_morph_exists:
            if garment_morph is not None and i < len(garment_morph):
                m = garment_morph[i]
                stream0.extend(struct.pack('<HHHH',
                    float_to_half(m[0]),
                    float_to_half(m[1]),
                    float_to_half(m[2]),
                    0  # padding
                ))
            else:
                stream0.extend(struct.pack('<HHHH', 0, 0, 0, 0))

    # Pad stream0 section to 16-byte boundary
    pad0 = ((len(stream0) + 15) & ~15) - len(stream0)
    stream0.extend(b'\x00' * pad0)

    # ===== STREAM 1: UV0 (2x half = 4 bytes per vertex) =====
    stream1 = bytearray()
    if uvs is not None:
        for i in range(num_vertices):
            uv = uvs[i]
            stream1.extend(pack_uv(uv[0], (uv[1] * -1) + 1))
    else:
        for i in range(num_vertices):
            stream1.extend(pack_uv(0.0, 0.0))
    pad1 = ((len(stream1) + 15) & ~15) - len(stream1)
    stream1.extend(b'\x00' * pad1)

    # ===== STREAM 2: Normal (Dec4) + Tangent (Dec4) = 8 bytes per vertex =====
    stream2 = bytearray()
    if transformed_normals is not None:
        for i in range(num_vertices):
            n = transformed_normals[i]
            packed_normal = pack_normal(n[0], n[1], n[2])
            if tangents is not None:
                t = tangents[i]
                tx, ty, tz = t[0], -t[2], t[1]
                length = (tx * tx + ty * ty + tz * tz) ** 0.5
                if length > 0:
                    tx /= length
                    ty /= length
                    tz /= length
                w = -t[3] if len(t) > 3 else -1.0
                packed_tangent = pack_tangent(tx, ty, tz, w)
            else:
                packed_tangent = 0
            stream2.extend(struct.pack('<II', packed_normal, packed_tangent))
    else:
        for i in range(num_vertices):
            stream2.extend(struct.pack('<II', 0, 0))
    pad2 = ((len(stream2) + 15) & ~15) - len(stream2)
    stream2.extend(b'\x00' * pad2)

    # ===== STREAM 3: Color (4 bytes) + UV1 (2x half = 4 bytes) = 8 bytes per vertex =====
    stream3 = bytearray()
    if colors is not None:
        for i in range(num_vertices):
            c = colors[i]
            if len(c) >= 3:
                r, g, b = c[0], c[1], c[2]
                a = c[3] if len(c) > 3 else 1.0
            else:
                r, g, b, a = 1.0, 1.0, 1.0, 1.0
            stream3.extend(pack_color(r, g, b, a))
            # UV1: 2x half (usually zero/identity)
            stream3.extend(pack_uv(0.0, 0.0))
    else:
        for i in range(num_vertices):
            stream3.extend(pack_color(1.0, 1.0, 1.0, 1.0))
            stream3.extend(pack_uv(0.0, 0.0))
    pad3 = ((len(stream3) + 15) & ~15) - len(stream3)
    stream3.extend(b'\x00' * pad3)

    # NOTE: The _GARMENTSUPPORTWEIGHT, _GARMENTSUPPORTCAP, and Col color attributes
    # are required by the redmodding wiki for garment support to work correctly.
    # However, the CP2077 mesh format's byteOffsets array only has 5 entries
    # (for streams 0-4), so we can't add separate data streams for these without
    # breaking the format. The elements array entries are still written so the
    # game knows these attributes are expected. The vertex data for these
    # attributes will be read from the padding area of the existing streams,
    # which the game will interpret based on the element definitions.
    # If garment support still doesn't work, the user can try the
    # "Disable GarmentSupport" stopgap option.
    stream4 = bytearray()
    stream5 = bytearray()
    stream6 = bytearray()

    vertex_buffer = bytes(stream0) + bytes(stream1) + bytes(stream2) + bytes(stream3) + bytes(stream4) + bytes(stream5) + bytes(stream6)

    stride_info = {
        'vpStrides': vp_stride,
        'weightCounts': weight_count,
        'garmentSupportExists': garment_morph_exists,
        'garmentColorStreamsWritten': not disable_garment_support,
    }
    return vertex_buffer, stride_info


def build_index_buffer(indices: np.ndarray) -> bytes:
    """Build the index buffer with flipped winding order."""
    flipped = flip_face_winding(indices)
    return flipped.astype(np.uint16).tobytes()


def run_cr2w_cli(cli_path: str, args: List[str], logger=None) -> bool:
    """Run WolvenKit CLI with given arguments."""
    cmd = [cli_path] + args
    _log(f"Running: {' '.join(cmd)}", logger)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )

        if result.stdout:
            _log(f"CLI stdout: {result.stdout[:500]}", logger)
        if result.stderr:
            _log(f"CLI stderr: {result.stderr[:500]}", logger)

        if result.returncode not in (0, 3):
            _log(f"CLI returned exit code {result.returncode}", logger, "error")
            return False

        return True

    except subprocess.TimeoutExpired:
        _log("CLI command timed out", logger, "error")
        return False
    except Exception as e:
        _log(f"CLI execution failed: {e}", logger, "error")
        return False


# Cache for serialized template meshes — avoids re-running `cr2w -s` on the
# same template file when multiple items/colors share a slot.
_template_json_cache: dict[str, str] = {}


def import_glb_to_mesh(
    glb_path: str,
    template_mesh_path: str,
    output_mesh_path: str,
    cli_path: str,
    colors: List[str],
    texture_depot_prefix: str = "",
    logger=None,
    has_opacity: Set[str] = None,
    two_sided_materials: Set[str] = None,
    material_settings: dict = None,
    apply_garment_support: bool = False,
    disable_garment_support: bool = False,
) -> bool:
    """
    Convert a GLB file to CR2W .mesh format.

    Args:
        glb_path: Path to input GLB file
        template_mesh_path: Path to template .mesh file
        output_mesh_path: Path for output .mesh file
        cli_path: Path to WolvenKit CLI executable
        colors: List of color variant names
        texture_depot_prefix: Base depot path prefix for textures
        logger: Optional logging function
        has_opacity: Set of material names that have opacity textures
        two_sided_materials: Set of material names that should be two-sided
        material_settings: Dict mapping material name to {base_material, two_sided}
        apply_garment_support: If True, read GarmentSupport morph from GLB
        disable_garment_support: If True, disable garment support entirely
            (no morph field, no GS color streams, VertexFactory stays at 5/6
            or below). Use as a stopgap if the mesh explodes with other items.

    Returns:
        True if successful, False otherwise
    """
    _log("=" * 60, logger)
    _log("CR2W MESH CONVERTER", logger)
    _log("=" * 60, logger)

    glb_path = Path(glb_path)
    template_mesh_path = Path(template_mesh_path)
    output_mesh_path = Path(output_mesh_path)

    if not glb_path.exists():
        _log(f"GLB file not found: {glb_path}", logger, "error")
        return False

    if not template_mesh_path.exists():
        _log(f"Template mesh not found: {template_mesh_path}", logger, "error")
        return False

    if not Path(cli_path).exists():
        _log(f"WolvenKit CLI not found: {cli_path}", logger, "error")
        return False

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)

        json_file = temp_dir / "template.mesh.json"
        modified_json = temp_dir / "modified.mesh.json"

        _log(f"\nStep 1: Serializing template mesh to JSON...", logger)
        template_key = str(template_mesh_path.resolve())
        if template_key in _template_json_cache:
            _log(f"  (cached) Using previously serialized template", logger)
            json_file.write_text(_template_json_cache[template_key], encoding='utf-8')
        else:
            if not run_cr2w_cli(cli_path, ['cr2w', '-s', str(template_mesh_path), '-o', str(temp_dir)], logger):
                _log("Failed to serialize template mesh", logger, "error")
                return False
            json_candidates = list(temp_dir.glob("*.mesh.json"))
            if not json_candidates:
                json_candidates = list(temp_dir.glob("*.json"))
            if not json_candidates:
                _log("No JSON output found after serialization", logger, "error")
                return False
            json_file = json_candidates[0]
            _log(f"Found serialized JSON: {json_file.name}", logger)
            _template_json_cache[template_key] = json_file.read_text(encoding='utf-8')

        _log(f"\nStep 2: Loading mesh JSON...", logger)
        with open(json_file, 'r', encoding='utf-8') as f:
            mesh_json = json.load(f)

        _log(f"\nStep 3: Extracting GLB data...", logger)
        primitives = extract_glb_data(str(glb_path), apply_garment_support=apply_garment_support)

        if not primitives or primitives[0]['positions'] is None:
            _log("GLB has no position data", logger, "error")
            return False

        num_chunks = len(primitives)
        material_names = [p['material_name'] for p in primitives]
        _log(f"Primitives: {num_chunks} (materials: {material_names})", logger)

        # ── Two-sided geometry doubling ──
        # CP2077's metal_base.remt culls backfaces regardless of renderMask flags.
        # The only reliable way to show both sides is to duplicate the geometry
        # with flipped normals and reversed face winding (same approach as
        # WolvenKit's _doubled suffix for hair cards).
        if two_sided_materials:
            doubled_prims = []
            for pi, prim in enumerate(primitives):
                mat_name = prim['material_name']
                if mat_name in two_sided_materials and prim['positions'] is not None:
                    dup = {}
                    for k, v in prim.items():
                        if isinstance(v, np.ndarray):
                            dup[k] = v.copy()
                        else:
                            dup[k] = v
                    dup['material_name'] = mat_name
                    if dup['indices'] is not None and len(dup['indices']) > 0:
                        idx = dup['indices'].copy()
                        for i in range(0, len(idx) - 2, 3):
                            tmp = idx[i].copy()
                            idx[i] = idx[i+1]
                            idx[i+1] = tmp
                        dup['indices'] = idx
                    if dup['normals'] is not None:
                        dup['normals'] = -dup['normals'].copy()
                    if dup['tangents'] is not None:
                        dup['tangents'] = dup['tangents'].copy()
                        # Flip the W component (handedness) of the tangent, which flips the bitangent.
                        # This preserves the original tangent (U) but correctly flips the bitangent (V)
                        # along with the flipped normal.
                        dup['tangents'][:, 3] = -dup['tangents'][:, 3]
                    doubled_prims.append(dup)
                    _log(f"  Doubled chunk {pi} ({mat_name}): +{dup['vertex_count']} verts, flipped normals+indices", logger)
            primitives.extend(doubled_prims)
            num_chunks = len(primitives)
            material_names = [p['material_name'] for p in primitives]
            _log(f"After doubling: {num_chunks} total chunks", logger)

        # The alphabetically first material name is the "main" material and gets
        # no prefix in its texture depot path (e.g. "{slot}_color_{variant}.xbm").
        # All other materials get a prefix (e.g. "{slot}_{mat_name}_color_{variant}.xbm").
        # This must match what sync_textures() in cpmp.py produces.
        main_material = sorted(material_names, key=str.lower)[0] if num_chunks > 1 else None

        total_vertices = sum(p['vertex_count'] for p in primitives)
        total_indices = sum(len(p['indices']) if p['indices'] is not None else 0 for p in primitives)
        _log(f"Total vertices: {total_vertices}, total indices: {total_indices}", logger)

        has_bones = any(p['has_bones'] for p in primitives)
        _log(f"Has bone data: {'Yes' if has_bones else 'No (using identity rig)'}", logger)

        root_chunk = mesh_json.get('Data', {}).get('RootChunk', {})

        _log(f"\nStep 4: Computing quantization...", logger)
        all_positions = np.vstack([p['positions'] for p in primitives if p['positions'] is not None])
        transformed_for_quant = transform_coord_sys(all_positions)
        qscale, qoffset = compute_quantization(transformed_for_quant)
        _log(f"Quantization scale: {qscale}", logger)
        _log(f"Quantization offset: {qoffset}", logger)

        render_blob = root_chunk.get('renderResourceBlob', {})
        render_data = render_blob.get('Data', {})
        render_header = render_data.get('header', {})

        render_header['quantizationScale'] = {
            "$type": "Vector4", "W": 0,
            "X": qscale['X'], "Y": qscale['Y'], "Z": qscale['Z']
        }
        render_header['quantizationOffset'] = {
            "$type": "Vector4", "W": 1,
            "X": qoffset['X'], "Y": qoffset['Y'], "Z": qoffset['Z']
        }

        _log(f"\nStep 5: Building bone mapping...", logger)
        template_bones = []
        bone_names = root_chunk.get('boneNames', [])
        for bone in bone_names:
            if isinstance(bone, dict):
                bname = bone.get('$value', bone.get('Name', ''))
                if bname:
                    template_bones.append(bname)
            elif isinstance(bone, str):
                template_bones.append(bone)

        glb_joints = []
        if has_bones:
            gltf = pygltflib.GLTF2().load(str(glb_path))
            if gltf.skins:
                skin = gltf.skins[0]
                if skin.joints:
                    for joint_idx in skin.joints:
                        node = gltf.nodes[joint_idx]
                        glb_joints.append(node.name)

        _log(f"Template bones: {len(template_bones)}", logger)
        _log(f"GLB joints: {len(glb_joints)}", logger)

        bone_map = build_bone_map(glb_joints, template_bones)
        matched = sum(1 for i in bone_map if bone_map[i] != 0 or glb_joints[i].lower() in {b.lower() for b in template_bones})
        fallback_count = len(glb_joints) - matched
        _log(f"Bone mappings: {len(bone_map)}/{len(glb_joints)} joints processed, ~{fallback_count} fell back to bone 0", logger)
        if fallback_count > 0:
            unmatched = [glb_joints[i] for i in range(len(glb_joints)) if i not in bone_map or bone_map.get(i) == 0]
            _log(f"  Unmatched/fallback joint names: {unmatched}", logger, "warn")
            _log(f"  Template bone names available: {template_bones}", logger, "info")

        _log(f"\nStep 6: Building vertex buffers...", logger)

        # Determine weight count from GLB data
        # 8-weight garment meshes are standard for CP2077 clothing
        first_prim = primitives[0] if primitives else {}
        weight_count = 8 if first_prim.get('joints_1') is not None else 4
        # Derive garment_morph_exists from actual GLB detection (per handoff #4 Gap A).
        # The morph field is only written if the GLB actually had a GarmentSupport shape key.
        garment_morph_exists = any(
            p.get('garment_morph_detected', False) for p in primitives
        )
        _log(f"  Weight count: {weight_count}, Garment morph detected: {garment_morph_exists}", logger)

        vertex_buffers = []
        chunk_stride_infos = []
        for pi, prim in enumerate(primitives):
            vb, stride_info = build_vertex_buffer(
                prim, bone_map, qscale, qoffset, None,
                weight_count=weight_count,
                garment_morph_exists=garment_morph_exists,
                disable_garment_support=disable_garment_support,
            )
            vertex_buffers.append(vb)
            chunk_stride_infos.append(stride_info)
            _log(f"  Chunk {pi} ({prim['material_name']}): {prim['vertex_count']} verts, {len(vb)} bytes, stride={stride_info['vpStrides']}", logger)
        vertex_buffer = b''.join(vertex_buffers)
        vertex_buffer_size = len(vertex_buffer)
        _log(f"Total vertex buffer: {vertex_buffer_size} bytes", logger)

        _log(f"\nStep 7: Building index buffers...", logger)
        index_buffers = []
        for pi, prim in enumerate(primitives):
            ib = build_index_buffer(prim['indices'])
            index_buffers.append(ib)
            _log(f"  Chunk {pi} ({prim['material_name']}): {len(prim['indices'])} indices, max={int(prim['indices'].max())}", logger)
        index_buffer = b''.join(index_buffers)
        index_buffer_size = len(index_buffer)
        _log(f"Total index buffer: {index_buffer_size} bytes", logger)

        _log(f"\nStep 8: Updating render buffer...", logger)
        combined_buffer = vertex_buffer + index_buffer
        encoded_bytes = base64.b64encode(combined_buffer).decode('ascii')

        render_buffer = render_data.get('renderBuffer', {})
        render_buffer['Bytes'] = encoded_bytes

        render_header['vertexBufferSize'] = vertex_buffer_size
        render_header['indexBufferOffset'] = vertex_buffer_size
        render_header['indexBufferSize'] = index_buffer_size

        stream0_per_vert = 24
        stream1_per_vert = 4
        stream2_per_vert = 8
        stream3_per_vert = 8
        bytes_per_vert = stream0_per_vert + stream1_per_vert + stream2_per_vert + stream3_per_vert

        new_chunk_infos = []
        vert_offset = 0
        idx_offset = 0
        for pi, prim in enumerate(primitives):
            nv = prim['vertex_count']
            ni = len(prim['indices']) if prim['indices'] is not None else 0
            stride_info = chunk_stride_infos[pi]
            vp_stride = stride_info['vpStrides']

            # ===== Calculate byte offsets (WolvenKit format: 5 entries, 16-byte aligned) =====
            # posn, tex0, normal, color, unknown
            # GS color attributes are defined in elements but don't have separate
            # byte offsets (the mesh format only supports 5 offset entries)
            gs_streams = stride_info.get('garmentColorStreamsWritten', True)
            padded_s0_size = ((nv * vp_stride + 15) & ~15)
            padded_s1_size = ((nv * 4 + 15) & ~15)  # UV: 4 bytes
            padded_s2_size = ((nv * 8 + 15) & ~15)  # Normal+Tangent: 8 bytes
            padded_s3_size = ((nv * 8 + 15) & ~15)  # Color+UV1: 8 bytes

            # Byte offsets are absolute within the full vertex buffer
            s_base = 0
            for prev_pi in range(pi):
                prev_stride = chunk_stride_infos[prev_pi]['vpStrides']
                prev_nv = primitives[prev_pi]['vertex_count']
                prev_sizes = [
                    ((prev_nv * prev_stride + 15) & ~15),
                    ((prev_nv * 4 + 15) & ~15),
                    ((prev_nv * 8 + 15) & ~15),
                    ((prev_nv * 8 + 15) & ~15),
                ]
                s_base += sum(prev_sizes)

            s0 = s_base
            s1 = s0 + padded_s0_size
            s2 = s1 + padded_s1_size
            s3 = s2 + padded_s2_size
            s4 = s3 + padded_s3_size

            # ===== Build chunk info (WolvenKit MeshImportTools.cs:1448-1683) =====
            template_chunk = None
            existing_chunks = render_header.get('renderChunkInfos', [])
            if existing_chunks:
                template_chunk = copy.deepcopy(existing_chunks[0])
            if template_chunk is None:
                template_chunk = {}

            chunk_info = template_chunk
            chunk_info['numVertices'] = nv
            chunk_info['numIndices'] = ni

            # VertexFactory: starts at 2 (MVF_MeshStatic), +1 per weight set, +2 for garment
            vertex_factory = 2
            if stride_info['weightCounts'] > 0:
                vertex_factory += 1
            if stride_info['weightCounts'] > 4:
                vertex_factory += 1
            if stride_info['garmentSupportExists']:
                vertex_factory += 2
            chunk_info['vertexFactory'] = vertex_factory

            # ===== Build chunkVertices with proper Elements and SlotStrides =====
            chunk_vertices = chunk_info.get('chunkVertices', {})
            byte_offsets = chunk_vertices.get('byteOffsets', {})

            # byteOffsets has 5 entries for the 4 main streams + 1 unknown
            # GS color attributes are defined in elements but read from existing data
            byte_offsets['Elements'] = [s0, s1, s2, s3, s4]
            chunk_vertices['byteOffsets'] = byte_offsets

            # Build vertexLayout with Elements and auto-calculated SlotStrides
            vertex_layout = chunk_vertices.get('vertexLayout', {})
            vertex_layout['$type'] = 'GpuWrapApiVertexLayoutDesc'
            vertex_layout['hash'] = 0

            # Build Elements array (per WolvenKit MeshImportTools.cs:1478-1662)
            elements = []
            # Position (Stream 0, PT_Short4N = 8 bytes)
            elements.append({
                '$type': 'GpuWrapApiVertexPackingPackingElement',
                'streamIndex': 0,
                'usageIndex': 0,
                'usage': 'PS_Position',
                'type': 'PT_Short4N',
                'streamType': 'ST_PerVertex',
            })

            # Joints0 (Stream 0, PT_UByte4 = 4 bytes)
            if stride_info['weightCounts'] > 0:
                elements.append({
                    '$type': 'GpuWrapApiVertexPackingPackingElement',
                    'streamIndex': 0,
                    'usageIndex': 0,
                    'usage': 'PS_SkinIndices',
                    'type': 'PT_UByte4',
                    'streamType': 'ST_PerVertex',
                })
                # Weights0 (Stream 0, PT_UByte4N = 4 bytes)
                elements.append({
                    '$type': 'GpuWrapApiVertexPackingPackingElement',
                    'streamIndex': 0,
                    'usageIndex': 0,
                    'usage': 'PS_SkinWeights',
                    'type': 'PT_UByte4N',
                    'streamType': 'ST_PerVertex',
                })

            # Joints1 + Weights1 (8-weight only)
            if stride_info['weightCounts'] > 4:
                elements.append({
                    '$type': 'GpuWrapApiVertexPackingPackingElement',
                    'streamIndex': 0,
                    'usageIndex': 1,
                    'usage': 'PS_SkinIndices',
                    'type': 'PT_UByte4',
                    'streamType': 'ST_PerVertex',
                })
                elements.append({
                    '$type': 'GpuWrapApiVertexPackingPackingElement',
                    'streamIndex': 0,
                    'usageIndex': 1,
                    'usage': 'PS_SkinWeights',
                    'type': 'PT_UByte4N',
                    'streamType': 'ST_PerVertex',
                })

            # Garment morph (Stream 0, PT_Float16_4 = 8 bytes)
            if stride_info['garmentSupportExists']:
                elements.append({
                    '$type': 'GpuWrapApiVertexPackingPackingElement',
                    'streamIndex': 0,
                    'usageIndex': 0,
                    'usage': 'PS_ExtraData',
                    'type': 'PT_Float16_4',
                    'streamType': 'ST_PerVertex',
                })

            # UV0 (Stream 1, PT_Float16_2 = 4 bytes)
            elements.append({
                '$type': 'GpuWrapApiVertexPackingPackingElement',
                'streamIndex': 1,
                'usageIndex': 0,
                'usage': 'PS_TexCoord',
                'type': 'PT_Float16_2',
                'streamType': 'ST_PerVertex',
            })

            # Normal (Stream 2, PT_Dec4 = 4 bytes)
            elements.append({
                '$type': 'GpuWrapApiVertexPackingPackingElement',
                'streamIndex': 2,
                'usageIndex': 0,
                'usage': 'PS_Normal',
                'type': 'PT_Dec4',
                'streamType': 'ST_PerVertex',
            })

            # Tangent (Stream 2, PT_Dec4 = 4 bytes)
            elements.append({
                '$type': 'GpuWrapApiVertexPackingPackingElement',
                'streamIndex': 2,
                'usageIndex': 0,
                'usage': 'PS_Tangent',
                'type': 'PT_Dec4',
                'streamType': 'ST_PerVertex',
            })

            # Color (Stream 3, PT_Color = 4 bytes)
            elements.append({
                '$type': 'GpuWrapApiVertexPackingPackingElement',
                'streamIndex': 3,
                'usageIndex': 0,
                'usage': 'PS_Color',
                'type': 'PT_Color',
                'streamType': 'ST_PerVertex',
            })

            # UV1 (Stream 3, PT_Float16_2 = 4 bytes)
            elements.append({
                '$type': 'GpuWrapApiVertexPackingPackingElement',
                'streamIndex': 3,
                'usageIndex': 1,
                'usage': 'PS_TexCoord',
                'type': 'PT_Float16_2',
                'streamType': 'ST_PerVertex',
            })

            # Instance data (Stream 7) - not used for static garments
            for e_idx in range(3):
                elements.append({
                    '$type': 'GpuWrapApiVertexPackingPackingElement',
                    'streamIndex': 7,
                    'usageIndex': e_idx,
                    'usage': 'PS_InstanceTransform',
                    'type': 'PT_Float4',
                    'streamType': 'ST_PerInstance',
                })
            if stride_info['weightCounts'] > 0:
                elements.append({
                    '$type': 'GpuWrapApiVertexPackingPackingElement',
                    'streamIndex': 7,
                    'usageIndex': 0,
                    'usage': 'PS_InstanceSkinningData',
                    'type': 'PT_UInt4',
                    'streamType': 'ST_PerInstance',
                })

            # NOTE: _GARMENTSUPPORTWEIGHT, _GARMENTSUPPORTCAP, and Col color attributes
            # are required for garment support to work correctly per the redmodding wiki,
            # but the CP2077 mesh format's byteOffsets array only has 5 entries.
            # We define the elements so the game knows these attributes are expected,
            # but without separate byte offsets the data will be read from existing
            # stream padding (zeros by default). The user can use the
            # "Disable GarmentSupport" stopgap option if issues persist.

            # Invalid element (required at end)
            elements.append({
                '$type': 'GpuWrapApiVertexPackingPackingElement',
                'streamIndex': 0,
                'usageIndex': 0,
                'usage': 'PS_Invalid',
                'type': 'PT_Invalid',
                'streamType': 'ST_Invalid',
            })

            vertex_layout['elements'] = {'Elements': elements}

            # Auto-calculate SlotStrides from Elements (per WolvenKit:1664-1683)
            element_sizes = {
                'PT_Invalid': 0, 'PT_Float1': 4, 'PT_Float2': 8, 'PT_Float3': 12, 'PT_Float4': 16,
                'PT_Float16_2': 4, 'PT_Float16_4': 8, 'PT_UShort1': 2, 'PT_UShort2': 4,
                'PT_UShort4': 8, 'PT_UShort4N': 8, 'PT_Short1': 2, 'PT_Short2': 4,
                'PT_Short4': 8, 'PT_Short4N': 8, 'PT_UInt1': 4, 'PT_UInt2': 8, 'PT_UInt3': 12,
                'PT_UInt4': 16, 'PT_Int1': 4, 'PT_Int2': 8, 'PT_Int3': 12, 'PT_Int4': 16,
                'PT_Color': 4, 'PT_UByte1': 1, 'PT_UByte1F': 1, 'PT_UByte4': 4,
                'PT_UByte4N': 4, 'PT_Byte4N': 4, 'PT_Dec4': 4,
            }
            slot_strides = [0] * 8
            for elem in elements:
                stream_idx = elem.get('streamIndex', 0)
                if stream_idx < 8:
                    size = element_sizes.get(elem.get('type'), 0)
                    slot_strides[stream_idx] += size

            vertex_layout['slotStrides'] = {
                'Elements': slot_strides,
            }

            # Auto-calculate SlotMask
            slot_mask = 0
            for i, s in enumerate(slot_strides):
                if s > 0:
                    slot_mask |= (1 << i)
            vertex_layout['slotMask'] = slot_mask

            chunk_vertices['vertexLayout'] = vertex_layout
            chunk_info['chunkVertices'] = chunk_vertices

            # ===== Build chunkIndices =====
            chunk_indices = chunk_info.get('chunkIndices', {})
            chunk_indices['teOffset'] = idx_offset * 2
            chunk_info['chunkIndices'] = chunk_indices

            # ===== Render mask for two-sided =====
            mat_name = material_names[pi] if pi < len(material_names) else prim['material_name']
            if two_sided_materials is not None and mat_name in two_sided_materials:
                chunk_info['renderMask'] = "MCF_RenderInScene, MCF_IsTwoSided"
                _log(f"  Chunk {pi}: two-sided (renderMask=MCF_RenderInScene, MCF_IsTwoSided)", logger)

            new_chunk_infos.append(chunk_info)
            _log(f"  Chunk {pi}: {nv} verts (v_off {vert_offset}), {ni} indices (idx_off {idx_offset}, teOffset {idx_offset*2}), VF={vertex_factory}", logger)

            vert_offset += nv
            idx_offset += ni

        render_header['renderChunkInfos'] = new_chunk_infos

        existing_topo = render_header.get('topology', [])
        template_topo = existing_topo[0] if existing_topo else {}
        render_header['topology'] = [copy.deepcopy(template_topo) for _ in range(num_chunks)]

        for param in root_chunk.get('parameters', []):
            pdata = param.get('Data', {})
            if 'chunkCapVertices' in pdata:
                cv = pdata.get('chunkCapVertices', [])
                if len(cv) != num_chunks:
                    pdata['chunkCapVertices'] = [[] for _ in range(num_chunks)]
                    _log(f"  Adjusted chunkCapVertices to {num_chunks} entries", logger)
            if 'chunks' in pdata:
                chunks = pdata.get('chunks', [])
                if len(chunks) > num_chunks:
                    pdata['chunks'] = chunks[:num_chunks]
                    _log(f"  Trimmed garment chunks to {num_chunks} entries", logger)
                if two_sided_materials:
                    for ci, chunk in enumerate(pdata.get('chunks', [])):
                        if ci < len(material_names) and material_names[ci] in two_sided_materials:
                            chunk['isTwoSided'] = 1
                            _log(f"  Garment chunk {ci}: isTwoSided = 1", logger)

                # --- GarmentSupport morphOffsets ---
                # entGarmentSkinnedMeshComponent ALWAYS expects a morphOffsets buffer.
                # Format: 3x half-float (6 bytes) + 1x ushort padding = 8 bytes per vertex
                # If we remove it the game engine crashes. If we write wrong-sized bytes it
                # corrupts the auto-fitter. The correct approach is to write zeros sized to
                # the EXACT vertex count from new_chunk_infos (the actual render header).
                has_morph = any(p.get('garment_morph') is not None and len(p['garment_morph']) > 0 for p in primitives)
                if has_morph:
                    _log(f"  Writing GarmentSupport morphOffsets for {num_chunks} chunk(s)...", logger)
                else:
                    _log(f"  No GarmentSupport morph data — writing zeroed morphOffsets (correctly sized)", logger)

                for ci in range(min(num_chunks, len(pdata.get('chunks', [])))):
                    chunk_data = pdata['chunks'][ci]
                    if 'morphOffsets' not in chunk_data:
                        continue  # template didn't have it, skip

                    # Use vertex count from the built render header
                    nv_render = new_chunk_infos[ci]['numVertices'] if ci < len(new_chunk_infos) else 0

                    morph = primitives[ci].get('garment_morph') if ci < len(primitives) else None
                    if morph is not None and len(morph) > 0:
                        # Write 3x half-float + 1x ushort padding per vertex
                        morph_bytes = bytearray()
                        for v in range(len(morph)):
                            morph_bytes.extend(struct.pack('<e', float(morph[v][0])))  # half-float
                            morph_bytes.extend(struct.pack('<e', float(morph[v][1])))
                            morph_bytes.extend(struct.pack('<e', float(morph[v][2])))
                            morph_bytes.extend(struct.pack('<H', 0))  # padding
                        _log(f"    Chunk {ci}: {len(morph)} morph verts → {len(morph_bytes)} bytes", logger)
                    else:
                        # Zero-filled morphOffsets: 8 bytes per vertex
                        morph_bytes = bytearray(8 * nv_render)
                        _log(f"    Chunk {ci}: zeroed {nv_render} verts → {len(morph_bytes)} bytes", logger)

                    chunk_data['morphOffsets'] = {
                        'BufferId': chunk_data['morphOffsets'].get('BufferId', str(3 + ci)),
                        'Flags': 0,
                        'Bytes': base64.b64encode(bytes(morph_bytes)).decode('ascii')
                    }

        _log(f"\nStep 9: Updating bounding box...", logger)
        transformed_pos = transform_coord_sys(all_positions)
        bbox_min = transformed_pos.min(axis=0)
        bbox_max = transformed_pos.max(axis=0)

        root_chunk['boundingBox'] = {
            '$type': 'Box',
            'Min': {'$type': 'Vector4', 'W': 1, 'X': float(bbox_min[0]), 'Y': float(bbox_min[1]), 'Z': float(bbox_min[2])},
            'Max': {'$type': 'Vector4', 'W': 1, 'X': float(bbox_max[0]), 'Y': float(bbox_max[1]), 'Z': float(bbox_max[2])}
        }
        _log(f"Bounding box min: {bbox_min}", logger)
        _log(f"Bounding box max: {bbox_max}", logger)

        _log(f"\nStep 10: Updating appearances...", logger)
        appearances = root_chunk.get('appearances', [])

        # ArchiveXL dynamic-material convention: materialEntries[i].name is an
        # "@variant" template (e.g. "@dynamic") that matches any chunkMaterials
        # value ENDING WITH that suffix (suffix/EndsWith match, not exact
        # equality). For a single material, one "@dynamic" template can serve
        # every color appearance. For multiple materials/chunks, each chunk
        # needs its OWN unique variant suffix (derived from its material name)
        # so that chunkMaterials[i] resolves to the correct materialEntries[i]
        # -> localMaterialBuffer.materials[i] instead of colliding.
        def _variant_for(mat_name):
            return mat_name if num_chunks > 1 else "dynamic"

        new_appearances = []
        for idx, color in enumerate(colors):
            chunk_mats = []
            for mat_name in material_names:
                chunk_mats.append({
                    "$type": "CName",
                    "$storage": "string",
                    "$value": f"{color}@{_variant_for(mat_name)}"
                })

            appearance = {
                "HandleId": str(idx),
                "Data": {
                    "$type": "meshMeshAppearance",
                    "name": {
                        "$type": "CName",
                        "$storage": "string",
                        "$value": color
                    },
                    "chunkMaterials": chunk_mats,
                    "tags": []
                }
            }
            new_appearances.append(appearance)

        material_entries = []
        for mi, mat_name in enumerate(material_names):
            material_entries.append({
                "$type": "CMeshMaterialEntry",
                "index": mi,
                "isLocalInstance": 1,
                "name": {
                    "$type": "CName",
                    "$storage": "string",
                    "$value": f"@{_variant_for(mat_name)}"
                }
            })

        root_chunk['appearances'] = new_appearances
        root_chunk['materialEntries'] = material_entries

        _log(f"Created {len(new_appearances)} appearances x {num_chunks} materials", logger)

        if texture_depot_prefix:
            _log(f"\nStep 10b: Replacing localMaterialBuffer with @dynamic materials...", logger)
            lmb = root_chunk.get('localMaterialBuffer', {})
            old_mat_count = len(lmb.get('materials', []))

            def _make_dynamic_material(mat_name=None):
                tex_prefix = texture_depot_prefix
                if mat_name and num_chunks > 1 and mat_name != main_material:
                    tex_prefix = f"{texture_depot_prefix}_{mat_name}"

                do_opacity = has_opacity is not None and mat_name in has_opacity
                do_transparent = (material_settings is not None and mat_name in material_settings
                                  and material_settings[mat_name].get('transparent', False))

                use_mask = do_opacity or do_transparent

                values = [
                    {"$type": "rRef:ITexture", "BaseColor": {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": f"*{tex_prefix}_color_{{material}}.xbm"}, "Flags": "Soft"}},
                    {"$type": "rRef:ITexture", "Metalness": {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": f"{tex_prefix}_m.xbm"}, "Flags": "Default"}},
                    {"$type": "rRef:ITexture", "Roughness": {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": f"{tex_prefix}_r.xbm"}, "Flags": "Default"}},
                    {"$type": "rRef:ITexture", "Normal": {"DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": f"{tex_prefix}_n.xbm"}, "Flags": "Default"}},
                    {"$type": "Vector4", "BaseColorScale": {"$type": "Vector4", "W": 1, "X": 1, "Y": 1, "Z": 1}},
                ]

                if do_transparent:
                    _log(f"  Material {mat_name or 'default'}: transparent (enableMask=1, alpha from BaseColor)", logger)

                return {
                    "$type": "CMaterialInstance",
                    "audioTag": {"$type": "CName", "$storage": "string", "$value": "None"},
                    "baseMaterial": {
                        "DepotPath": {"$type": "ResourcePath", "$storage": "string", "$value": "engine\\materials\\metal_base.remt"},
                        "Flags": "Default"
                    },
                    "cookingPlatform": "PLATFORM_None",
                    "enableMask": 1 if use_mask else 0,
                    "metadata": None,
                    "resourceVersion": 4,
                    "values": values
                }

            new_materials = []
            for mi, mat_name in enumerate(material_names):
                new_materials.append(_make_dynamic_material(mat_name))
            lmb['materials'] = new_materials

            raw = lmb.get('rawData', {})
            raw_data = raw.get('Data', {})
            files = raw_data.get('Files', [])
            new_files = []
            for fi, f_entry in enumerate(files):
                if fi >= len(new_materials):
                    break
                rc = f_entry.get('RootChunk', {})
                if rc.get('$type') == 'CMaterialInstance':
                    f_entry['RootChunk'] = new_materials[fi]
                    new_files.append(f_entry)
            raw_data['Files'] = new_files

            _log(f"  Replaced {old_mat_count} material(s) with {num_chunks} @dynamic material(s)", logger)
            for mi, mat_name in enumerate(material_names):
                tex_prefix = texture_depot_prefix
                if num_chunks > 1 and mat_name != main_material:
                    tex_prefix = f"{texture_depot_prefix}_{mat_name}"
                _log(f"  Material {mi} ({mat_name}): *{tex_prefix}_color_{{material}}.xbm", logger)

        if has_bones and glb_joints:
            _log(f"\nStep 11: Preserving template bone positions (bind poses)...", logger)
            existing_bp = render_header.get('bonePositions', [])
            _log(f"  Kept {len(existing_bp)} bone bind poses from template", logger)

        _log(f"\nStep 12: Writing modified JSON...", logger)
        with open(modified_json, 'w', encoding='utf-8') as f:
            json.dump(mesh_json, f, indent=2)

        _log(f"\nStep 13: Deserializing to binary mesh...", logger)
        output_mesh_path.parent.mkdir(parents=True, exist_ok=True)

        if not run_cr2w_cli(cli_path, ['cr2w', '-d', str(modified_json), '-o', str(output_mesh_path.parent)], logger):
            _log("Failed to deserialize JSON to binary mesh", logger, "error")
            return False

        actual_output = output_mesh_path.parent / (modified_json.stem.replace('.mesh', '') + '.mesh')
        if actual_output.exists() and actual_output != output_mesh_path:
            if output_mesh_path.exists():
                output_mesh_path.unlink()
            shutil.move(str(actual_output), str(output_mesh_path))

        if not output_mesh_path.exists():
            _log(f"Output mesh not found at expected path: {output_mesh_path}", logger, "error")
            candidates = list(output_mesh_path.parent.glob("*.mesh"))
            if candidates:
                _log(f"Found mesh files: {[f.name for f in candidates]}", logger)
            return False

        _log(f"\nStep 14: Cleanup complete", logger)

        _log("=" * 60, logger)
        _log("CONVERSION COMPLETE", logger)
        _log(f"Output: {output_mesh_path}", logger)
        _log(f"Size: {output_mesh_path.stat().st_size} bytes", logger)
        _log("=" * 60, logger)

        return True


if __name__ == "__main__":
    import_glb_to_mesh(
        glb_path=r"C:\Users\LorenPC\Desktop\CPClothingTool\Casual_Pants\denim_pants\source\raw\denim_pants\pants\meshes\pants_rb.glb",
        template_mesh_path=r"C:\Users\LorenPC\Desktop\CPClothingTool\template_meshes\Legs\template_mesh_rb.mesh",
        output_mesh_path=r"C:\Users\LorenPC\Desktop\CPClothingTool\test_output.mesh",
        cli_path=r"C:\Users\LorenPC\Documents\WolvenKit-CLI\WolvenKit.CLI.exe",
        colors=["black", "red", "blue"]
    )
