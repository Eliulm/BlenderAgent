"""Run many products through run.py in parallel, resumably.

python batch.py --queue queue.jsonl --root /data/batch_01 --workers 20 --gpus 1,2

Each queue line is {"name": "unique_slug", "product": "Exact product name"} and
may carry extra keys (category, notes). A finished item has
<root>/output/<name>/asset.blend and is skipped; anything else is retried with
--resume. Per-item logs go to <root>/logs/<name>.log and one JSON line per
attempt is appended to <root>/status.jsonl. Rerun the same command after a
crash or to pick up new queue lines.

--stage research stops after <root>/research/<name>/evidence.json; --stage
build only starts items whose evidence already exists. With --watch SECONDS a
build batch keeps polling for newly arrived evidence (for example synced from
another machine) until every item is done or has been attempted once.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import itertools
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parent
LOCK=threading.Lock()


def _queue(path):
    items,seen=[],set()
    for number,line in enumerate(Path(path).read_text().splitlines(),1):
        if not line.strip():continue
        item=json.loads(line)
        if not item.get('name') or not item.get('product'):
            raise ValueError(f'queue line {number} needs name and product')
        if item['name'] in seen:
            raise ValueError(f'duplicate queue name {item["name"]}')
        seen.add(item['name'])
        items.append(item)
    return items


def _log(root,record):
    with LOCK, open(root/'status.jsonl','a') as stream:
        stream.write(json.dumps(record)+'\n')


def _done(item,args):
    target=('research',item['name'],'evidence.json') if args.stage=='research' else \
        ('output',item['name'],'asset.blend')
    return args.root.joinpath(*target).is_file()


def _ready(item,args):
    return args.stage!='build' or (args.root/'research'/item['name']/'evidence.json').is_file()


def _run(item,args,gpu):
    root=args.root
    if _done(item,args):
        return item['name'],'skipped'
    env=dict(os.environ)
    if gpu is not None:
        env.update(BA_CYCLES_DEVICE='OPTIX',CUDA_VISIBLE_DEVICES=str(gpu))
    cmd=[sys.executable,str(ROOT/'run.py'),'--product',item['product'],'--name',item['name'],
         '--root',str(root),'--model',args.model,'--effort',args.effort,'--resume']
    if args.stage=='research':cmd.append('--research-only')
    for attempt in range(args.retries+1):
        if attempt and args.stage=='research':
            # A failed research run restarts clean rather than resuming its faulty response.
            shutil.rmtree(root/'research'/item['name'],ignore_errors=True)
        start=time.time()
        with open(root/'logs'/f'{item["name"]}.log','a') as log:
            log.write(f'\n=== {time.strftime("%Y-%m-%d %H:%M:%S")} stage={args.stage} '
                      f'attempt={attempt+1} gpu={gpu}\n')
            log.flush()
            code=subprocess.run(cmd,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,
                                check=False).returncode
        status='done' if _done(item,args) else 'failed'
        _log(root,dict(name=item['name'],stage=args.stage,status=status,code=code,gpu=gpu,
                       attempt=attempt+1,seconds=round(time.time()-start),
                       finished=time.strftime('%Y-%m-%dT%H:%M:%S')))
        if status=='done':break
    return item['name'],status


def main():
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--queue',required=True,type=Path)
    parser.add_argument('--root',required=True,type=Path,help='Batch directory for research/, output/, logs/')
    parser.add_argument('--workers',type=int,default=8)
    parser.add_argument('--gpus',default='',help='Comma-separated CUDA indices for rendering; empty renders on CPU')
    parser.add_argument('--model',default='gpt-6.1-sol',help='Builder model')
    parser.add_argument('--effort',choices=('low','medium','high','xhigh'),default='xhigh')
    parser.add_argument('--limit',type=int,help='Only start the first N unfinished items')
    parser.add_argument('--stage',choices=('all','research','build'),default='all')
    parser.add_argument('--retries',type=int,default=2,
                        help='Extra attempts per failed item; research retries start clean')
    parser.add_argument('--watch',type=int,metavar='SECONDS',
                        help='Build stage: keep polling for new evidence at this interval')
    args=parser.parse_args()
    args.root=args.root.expanduser().resolve()
    (args.root/'logs').mkdir(parents=True,exist_ok=True)
    items=_queue(args.queue)
    todo=[i for i in items if not _done(i,args)]
    if args.limit is not None:todo=todo[:args.limit]
    gpus=itertools.cycle([int(g) for g in args.gpus.split(',') if g.strip()] or [None])
    print(f'{len(items)} queued, {len(items)-len(todo)} already done, {len(todo)} to run '
          f'(stage {args.stage}, {args.workers} workers)',flush=True)
    counts,started,running={},set(),set()
    with ThreadPoolExecutor(args.workers) as pool:
        while True:
            for item in todo:
                if item['name'] not in started and len(running)<args.workers and _ready(item,args):
                    started.add(item['name'])
                    running.add(pool.submit(_run,item,args,next(gpus)))
            waiting=len(todo)-len(started)
            if not running and (not args.watch or not waiting):
                break
            finished,_=wait(running,timeout=args.watch,return_when=FIRST_COMPLETED) if running \
                else (set(),None)
            if not running:time.sleep(args.watch)
            for future in finished:
                running.discard(future)
                name,status=future.result()
                counts[status]=counts.get(status,0)+1
                print(f'{status:7s} {name}  ({sum(counts.values())}/{len(todo)}, '
                      f'{waiting} waiting for evidence)',flush=True)
    if len(started)<len(todo):
        print(f'{len(todo)-len(started)} items never had evidence',flush=True)
    print(json.dumps(counts))
    return 0 if not counts.get('failed') else 1


if __name__=='__main__':
    raise SystemExit(main())
