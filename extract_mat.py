import json, sys

with open(sys.argv[1]) as f:
    data = json.load(f)

rc = data['Data']['RootChunk']
lmb = rc['localMaterialBuffer']
mat = lmb['materials'][0]
print('baseMaterial:', mat['baseMaterial']['DepotPath']['$value'])

for v in mat['values']:
    vtype = v.get('$type', '')
    if vtype == 'rRef:ITexture':
        for key in ['BaseColor', 'Normal', 'Roughness', 'Metalness']:
            if key in v:
                dp = v[key]['DepotPath']['$value']
                flags = v[key].get('Flags', '')
                print(f'  {key}: {dp}  [Flags={flags}]')
    elif vtype == 'Vector4' and 'BaseColorScale' in v:
        print(f'  BaseColorScale: X={v["X"]} Y={v["Y"]} Z={v["Z"]} W={v["W"]}')
