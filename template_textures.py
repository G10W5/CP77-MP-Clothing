"""Extract the texture depot paths from a CR2W mesh's localMaterialBuffer.
Returns dict: {role: depot_path} where role is 'BaseColor', 'Normal', 'Roughness', 'Metalness'.
Also returns the depot path prefix for template texture aliasing."""
import json
import subprocess
import tempfile
import re
from pathlib import Path


def extract_template_texture_paths(mesh_path: str, cli_path: str) -> dict:
    """Serialize a .mesh to JSON and extract its localMaterialBuffer texture depot paths."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [cli_path, 'cr2w', '-s', mesh_path, '-o', tmp]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        mesh_name = Path(mesh_path).stem
        json_path = Path(tmp) / f'{mesh_name}.mesh.json'
        if not json_path.exists():
            # Try finding any .json in the dir
            jsons = list(Path(tmp).glob('*.mesh.json'))
            if not jsons:
                return {}
            json_path = jsons[0]
        
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)
    
    rc = data.get('Data', data).get('RootChunk', data.get('RootChunk', {}))
    lmb = rc.get('localMaterialBuffer', {})
    materials = lmb.get('materials', [])
    
    if not materials:
        return {}
    
    mat = materials[0]
    paths = {}
    for v in mat.get('values', []):
        if v.get('$type') == 'rRef:ITexture':
            for key in ['BaseColor', 'Normal', 'Roughness', 'Metalness']:
                if key in v:
                    dp = v[key]['DepotPath']['$value']
                    flags = v[key].get('Flags', 'Default')
                    paths[key] = {'depot_path': dp, 'flags': flags}
    
    return paths


def build_texture_copy_map(template_paths: dict, our_prefix: str, colors: list) -> list:
    """Build a list of (src_filename, dst_depot_path) for copying .xbm files.
    
    template_paths: from extract_template_texture_paths()
    our_prefix: e.g. 'glower_set\\pants\\textures\\pants' (our archive paths)
    colors: list of color variant names
    
    Returns list of (src_xbm_name, dst_depot_subpath) tuples.
    """
    copies = []
    
    for role, info in template_paths.items():
        dp = info['depot_path']
        # Strip leading * if present (ArchiveXL marker)
        clean_dp = dp.lstrip('*')
        
        if role == 'BaseColor':
            # BaseColor uses {material} placeholder for ArchiveXL dynamic substitution
            # e.g. *arcadie_outfit\1_pants\textures\arcadie_pants_color_{material}.xbm
            # For each color, copy our_{color}.xbm -> template_color_{color}.xbm
            for color in colors:
                # Build the template depot path for this specific color
                concrete_dp = clean_dp.replace('{material}', color)
                src_name = f"{our_prefix.split(chr(92))[-1]}_color_{color}.xbm"
                copies.append((src_name, concrete_dp))
        else:
            # Normal/Roughness/Metalness are static paths
            src_suffix = {'Normal': 'n', 'Roughness': 'r', 'Metalness': 'm'}[role]
            src_name = f"{our_prefix.split(chr(92))[-1]}_{src_suffix}.xbm"
            copies.append((src_name, clean_dp))
    
    return copies


if __name__ == '__main__':
    import sys
    mesh = sys.argv[1]
    cli = sys.argv[2]
    paths = extract_template_texture_paths(mesh, cli)
    print(f"Template: {mesh}")
    for role, info in paths.items():
        print(f"  {role}: {info['depot_path']} [Flags={info['flags']}]")
