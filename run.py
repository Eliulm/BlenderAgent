"""Research a product with Luna, then build it with Astra and Blender.

python run.py --product 'Exact product name' --name product_slug
python run.py --evidence /path/to/evidence.json --name product_slug
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    target=parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--product',help='Exact product name to research with GPT-6 Luna')
    target.add_argument('--evidence',type=Path,help='Existing researched evidence.json')
    parser.add_argument('--name',required=True,help='Unique output directory name')
    parser.add_argument('--effort',choices=('low','medium','high'),default='low')
    parser.add_argument('--preview-width',type=int,default=1440,
                        help='Contact-sheet width in pixels; default 1440 (use 2100 for full size)')
    parser.add_argument('--reference-max-side',type=int,default=1260,
                        help='Longest side of images attached to Luna and Astra; default 1260')
    parser.add_argument('--keep-work',action='store_true',help='Keep builder prompts and attempts')
    parser.add_argument('--keep-research-audit',action='store_true')
    parser.add_argument('--resume',action='store_true',help='Resume a checkpointed builder run')
    args=parser.parse_args()
    if args.product:
        evidence=ROOT/'research'/args.name/'evidence.json'
        if not evidence.is_file():
            cmd=[sys.executable,str(ROOT/'researcher'/'runner.py'),args.product,'--name',args.name]
            cmd+=['--image-max-side',str(args.reference_max_side)]
            if args.keep_research_audit:cmd.append('--keep-audit')
            if args.resume:cmd.append('--resume')
            if subprocess.run(cmd,cwd=ROOT,check=False).returncode:
                return 1
    else:
        evidence=args.evidence.expanduser().resolve()
    cmd=[sys.executable,str(ROOT/'builder'/'runner.py'),
         '--evidence',str(evidence),'--name',args.name,'--effort',args.effort,
         '--width',str(args.preview_width),'--reference-max-side',str(args.reference_max_side)]
    if args.keep_work:cmd.append('--keep-work')
    if args.resume and (ROOT/'output'/args.name/'.work'/'state.json').is_file():
        cmd.append('--resume')
    return subprocess.run(cmd,cwd=ROOT,check=False).returncode


if __name__=='__main__':
    raise SystemExit(main())
