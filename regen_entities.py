#!/usr/bin/env python3
"""Regenerate entity files for couture_doll project."""
import sys, json, copy, os
sys.path.insert(0, r'C:\Users\LorenPC\Desktop\CPClothingTool')
from cpmp import PipelineWorker

from pathlib import Path
worker = PipelineWorker(log_fn=lambda msg, lvl='info': None)
worker.script_dir = Path(r'C:\Users\LorenPC\Desktop\CPClothingTool')

items = [
    {
        'name': 'bottom',
        'slot': 'GenericLegClothing',
        'eq_slot': 'OutfitSlots.LegsMiddle',
        'enabled': True,
        'has_foot_variants': True,
        'foot_states': ['flat', 'lifted', 'heel'],
        'found_variants': ['espresso', 'emerald', 'crimson', 'midnight', 'royal'],
    },
    {
        'name': 'top',
        'slot': 'GenericTorsoClothing',
        'eq_slot': 'OutfitSlots.TorsoMiddle',
        'enabled': True,
        'has_foot_variants': False,
        'foot_states': [],
        'found_variants': ['espresso', 'emerald', 'crimson', 'midnight', 'royal'],
    },
]

colors = ['espresso', 'emerald', 'crimson', 'midnight', 'royal']
mod_base = 'couture_doll'
output_dir = r'C:\Users\LorenPC\Desktop\CPClothingTool\Projects'

worker.compile_json_templates(output_dir, mod_base, items, colors, None)
print('Entity generation complete!')

for item in items:
    name = item['name']
    d = os.path.join(output_dir, mod_base, 'source', 'archive', mod_base, name)
    if os.path.exists(d):
        files = sorted(os.listdir(d))
        print(f'\n{name}/:')
        for f in files:
            print(f'  {f}')
