#!/usr/bin/env python3
"""Fix slotStrides in deployed mesh files and re-serialize."""
import json
import subprocess
import os

WKIT = r'C:\Users\LorenPC\Documents\WolvenKit-CLI\WolvenKit.CLI.exe'
DIRS = [
    r'C:\Users\LorenPC\Desktop\CPClothingTool\Projects\couture_doll\source\archive\couture_doll\bottom\meshes',
    r'C:\Users\LorenPC\Desktop\CPClothingTool\Projects\couture_doll\source\archive\couture_doll\top\meshes',
]

for d in DIRS:
    for fname in sorted(os.listdir(d)):
        if not fname.endswith('.mesh'):
            continue
        mesh_path = os.path.join(d, fname)
        json_path = mesh_path + '.json'

        # Serialize binary -> JSON
        subprocess.run([WKIT, 'cr2w', '-s', mesh_path], capture_output=True)

        if not os.path.exists(json_path):
            print(f'  SKIP: {fname}')
            continue

        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        rrd = data.get('Data', {}).get('RootChunk', {}).get('renderResourceBlob', {}).get('Data', {})
        header = rrd.get('header', {})
        h_rci = header.get('renderChunkInfos', [])

        fixed = False
        for chunk in h_rci:
            if not isinstance(chunk, dict):
                continue
            cv = chunk.get('chunkVertices', {})
            vl = cv.get('vertexLayout', {})
            if not isinstance(vl, dict):
                continue
            ss = vl.get('slotStrides', {})
            if not isinstance(ss, dict):
                continue
            elems = ss.get('Elements', [])
            if elems and len(elems) > 0 and elems[0] != 24:
                old = elems[0]
                elems[0] = 24   # stream0_per_vert
                elems[1] = 4    # stream1_per_vert
                elems[2] = 8    # stream2_per_vert
                elems[3] = 8    # stream3_per_vert
                fixed = True
                print(f'  {fname}: slotStrides[0] {old} -> 24')

        if fixed:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            subprocess.run([WKIT, 'cr2w', '-d', json_path], capture_output=True)
            print(f'  Re-serialized: {fname}')
        else:
            print(f'  {fname}: slotStrides already correct or not found')
