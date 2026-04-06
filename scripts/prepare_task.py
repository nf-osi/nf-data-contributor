#!/usr/bin/env python3
"""Prepare the daily task prompt by injecting runtime variables into the template."""
import os
import pathlib
import yaml

with open('config/settings.yaml') as f:
    cfg = yaml.safe_load(f)

workspace_dir = cfg['agent']['workspace_dir']
pathlib.Path(workspace_dir).mkdir(parents=True, exist_ok=True)

template = pathlib.Path('prompts/daily_task_template.md').read_text()
template = template.replace('{{TODAY}}', os.environ['TODAY'])
template = template.replace('{{LOOKBACK_DATE}}', os.environ['LOOKBACK_DATE'])
template = template.replace('{{SEED_ID}}', os.environ.get('SEED_ID', ''))
template = template.replace('{WORKSPACE_DIR}', workspace_dir)

out = pathlib.Path(workspace_dir) / 'daily_task.md'
out.write_text(template)
print(f"Task prompt written to {out}")
