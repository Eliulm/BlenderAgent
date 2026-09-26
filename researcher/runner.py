"""Use GPT-6 Luna to research a named object and download a compact evidence set.

python researcher/runner.py 'Exact product name' --name output_slug
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
from string import Template
import subprocess
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from image_utils import downscale_images
IMAGE_LIMIT=10
MAX_BYTES=10_000_000
MAX_SEARCH_TURNS=4
SCHEMA={
    'type':'object','additionalProperties':False,
    'properties':{
        'product':{'type':'string'},
        'complexity':{'type':'string','enum':['simple','complex']},
        'internal_evidence_status':{'type':'string','enum':['not_needed','found','unavailable']},
        'images':{'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{
                'id':{'type':'string'},'url':{'type':'string'},'source_url':{'type':'string'},
                'path':{'type':'string'},
                'kind':{'type':'string','enum':['exterior','interior','dimension']},
                'description':{'type':'string'}},
            'required':['id','url','source_url','path','kind','description']}},
        'parts':{'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{
                'name':{'type':'string'},'material':{'type':'string'},
                'location':{'type':'string'},'reason':{'type':'string'},
                'basis':{'type':'string','enum':['observed','inferred']},
                'evidence':{'type':'array','items':{'type':'string'}}},
            'required':['name','material','location','reason','basis','evidence']}},
        'dimensions':{'type':'array','items':{'type':'object','additionalProperties':False,
            'properties':{'label':{'type':'string'},'value_mm':{'type':'number'},
                          'source_url':{'type':'string'}},
            'required':['label','value_mm','source_url']}},
        'notes':{'type':'array','items':{'type':'string'}},
        'gaps':{'type':'array','items':{'type':'string'}},
    },
    'required':['product','complexity','internal_evidence_status','images','parts',
                'dimensions','notes','gaps'],
}


def _image_type(data):
    if data.startswith(b'\xff\xd8\xff'):return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return '.png'
    if data[:4]==b'RIFF' and data[8:12]==b'WEBP':return '.webp'
    return None


def _validate(report):
    if not isinstance(report,dict) or set(report)!=set(SCHEMA['required']):
        raise ValueError('research result missing required fields')
    images=report['images']
    if not isinstance(images,list) or not 1<=len(images)<=IMAGE_LIMIT:
        raise ValueError('researcher must select 1–10 images')
    if not isinstance(report['parts'],list) or not report['parts']:
        raise ValueError('researcher returned no required parts')
    for item in report['parts']:
        wording=' '.join((item.get('name',''),item.get('reason',''))).lower()
        if re.search(r'\boptional\b|include only if|if modeling the|if modelling the',wording):
            raise ValueError('required-parts list contains a conditional part: '+item.get('name',''))
    if not any(item.get('kind')=='exterior' for item in images):
        raise ValueError('at least one exterior image is required')
    for index,item in enumerate(images,1):
        if item.get('id')!=f'image_{index:02d}' or not item.get('description'):
            raise ValueError('image IDs must be consecutive and described')
        if not item.get('source_url') or not item.get('url') or not item.get('path'):
            raise ValueError('image source, download URL, or local path is missing')
    for dim in report['dimensions']:
        if dim['value_mm']<=0 or not dim['source_url']:
            raise ValueError('dimensions need positive mm values and source URLs')


def _local_images(report,out,previous=None,not_before_ns=None):
    """Check Luna's files; never trust a path or an image URL without local bytes."""
    prior={item['id']:(item['url'],item['path']) for item in previous['images']} if previous else {}
    failures=[]
    paths=[]
    for item in report['images']:
        relative=Path(item['path'])
        if len(relative.parts)!=2 or relative.parts[0]!='images' or \
                relative.name!=item['id']+relative.suffix or \
                relative.suffix.lower() not in ('.jpg','.png','.webp'):
            failures.append(item['id']+': path must be images/'+item['id']+'.jpg/.png/.webp')
            continue
        path=(out/relative).resolve()
        if not path.is_relative_to((out/'images').resolve()) or not path.is_file():
            failures.append(item['id']+': local image missing at '+str(relative))
            continue
        if not_before_ns and prior.get(item['id']) not in (None,(item['url'],item['path'])) and \
                path.stat().st_mtime_ns<not_before_ns:
            failures.append(item['id']+': URL changed but local image was not replaced')
            continue
        size=path.stat().st_size
        if not 32<=size<=MAX_BYTES:
            failures.append(item['id']+': image size invalid ('+str(size)+' bytes)')
            continue
        with path.open('rb') as stream:header=stream.read(16)
        if _image_type(header)!=relative.suffix.lower():
            failures.append(item['id']+': file is not a valid '+relative.suffix+' image')
            continue
        paths.append(path)
    return paths,failures


def _call_codex(args,prompt,out,phase,thread=None,images=(),visual=False):
    path=out/f'research_turn_{phase:02d}'
    path.mkdir()
    (path/'prompt.txt').write_text(prompt)
    final=path/'response.json'
    base=['-m','gpt-6-luna','-c','model_reasoning_effort="low"',
          '-c',f'web_search="{"disabled" if visual else "live"}"',
          '-c',f'sandbox_mode="{"read-only" if visual else "workspace-write"}"',
          '-c','approval_policy="never"',
          '--skip-git-repo-check','--json','--output-schema',str(out/'schema.json'),
          '-o',str(final)]
    if not visual:base+=['-c','sandbox_workspace_write.network_access=true']
    cmd=([args.codex_bin,'exec',*base,'-C',str(out)] if thread is None else
         [args.codex_bin,'exec','resume',*base])
    for image in images:cmd += ['-i',str(image)]
    if thread is not None:cmd.append(thread)
    cmd.append('-')
    with (path/'prompt.txt').open('rb') as stdin, (path/'events.jsonl').open('wb') as stdout, \
            (path/'stderr.log').open('wb') as stderr:
        proc=subprocess.run(cmd,cwd=ROOT,stdin=stdin,stdout=stdout,stderr=stderr,
                            timeout=args.timeout,check=False)
    if proc.returncode or not final.is_file():
        raise RuntimeError(f'Luna research turn failed; inspect {path / "stderr.log"}')
    events=[]
    for line in (path/'events.jsonl').read_text().splitlines():
        try:events.append(json.loads(line))
        except json.JSONDecodeError:pass
    identifier=next((e['thread_id'] for e in events if e.get('type')=='thread.started'),thread)
    if not identifier or not any(e.get('type')=='turn.completed' for e in events):
        raise RuntimeError(f'Luna research turn incomplete; inspect {path}')
    if visual and any(e.get('type') in ('item.started','item.completed') and
                      e.get('item',{}).get('type') not in ('reasoning','agent_message','error')
                      for e in events):
        raise RuntimeError('visual audit unexpectedly called a tool')
    usage=next((e.get('usage',{}) for e in reversed(events) if e.get('type')=='turn.completed'),{})
    searches=sum(e.get('type')=='item.completed' and e.get('item',{}).get('type')=='web_search'
                 for e in events)
    return identifier,json.loads(final.read_text()),usage,searches


def run(args):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',args.name):
        raise ValueError('--name must be a simple unique directory name')
    out=(Path(args.output).expanduser().resolve() if args.output else ROOT/'research')/args.name
    out.mkdir(parents=True,exist_ok=args.resume)
    (out/'schema.json').write_text(json.dumps(SCHEMA,indent=2))
    report=None
    failures=[]
    usage_records=[]
    search_calls=0
    search_turns=0
    phase=0
    if args.resume:
        turns=sorted(out.glob('research_turn_*'))
        if not turns or not (turns[-1]/'response.json').is_file():
            raise ValueError('no completed research response to resume')
        report=json.loads((turns[-1]/'response.json').read_text())
        failures=(out/'download_errors.txt').read_text().splitlines() if (out/'download_errors.txt').is_file() else []
        phase=max(int(path.name.rsplit('_',1)[-1]) for path in turns)+1
        for path in turns:
            events=[]
            for line in (path/'events.jsonl').read_text().splitlines():
                try:events.append(json.loads(line))
                except json.JSONDecodeError:pass
            usage_records.append(next((e.get('usage',{}) for e in reversed(events)
                                       if e.get('type')=='turn.completed'),{}))
            search_calls+=sum(e.get('type')=='item.completed' and
                              e.get('item',{}).get('type')=='web_search' for e in events)
            search_turns+=1
    images_dir=out/'images'
    images_dir.mkdir(exist_ok=True)
    base_prompt=Template((HERE/'prompt.txt').read_text()).substitute(
        PRODUCT=args.product,IMAGE_DIR=images_dir)
    while search_turns<MAX_SEARCH_TURNS:
        previous_report=report
        started_ns=None
        if report is None or failures:
            if report is None:
                prompt=base_prompt
            else:
                prompt=(
                    base_prompt+'\nRepair the COMPLETE result below. Keep useful sourced '
                    'research and already valid local images. For missing or corrupt files, '
                    'download replacements yourself into the image directory before returning. '
                    'If a host blocks downloads, use a source-page image, thumbnail or a '
                    'different host. A changed URL must have a newly written local image. '
                    'Return the full corrected JSON with consecutive image IDs.\nFailures:\n'+
                    '\n'.join(failures)+'\nPrevious result:\n'+json.dumps(report))
            _,report,usage,calls=_call_codex(args,prompt,out,phase)
            started_ns=(out/f'research_turn_{phase:02d}'/'prompt.txt').stat().st_mtime_ns
            usage_records.append(usage)
            search_calls+=calls
            search_turns+=1
            phase+=1
        try:_validate(report)
        except ValueError as exc:
            failures=[str(exc)]
            (out/'download_errors.txt').write_text('\n'.join(failures)+'\n')
            continue
        downloaded,failures=_local_images(report,out,previous_report,started_ns)
        if not failures:
            audit_images=downscale_images(downloaded,out/'audit_images',args.image_max_side)
            audit_prompt=(
                'Visually audit the ATTACHED downloaded images in their image_01, image_02, '
                'etc. order. Attachments are bounded-size copies of the saved originals. '
                'Here is the draft research evidence:\n'+json.dumps(report,indent=2)+
                '\nReturn a corrected COMPLETE JSON result using the same schema. Inspect '
                'the actual pixels, correct every view description and part inference, and '
                'do not cite an image for internals it does not show. '
                'distinguish a removable accessory from the bare product. The parts array '
                'must contain only required physical regions of the bare product; merge '
                'same-material continuous features. Correct unsupported dimensional claims '
                'rather than inventing values. Preserve measurements cited to a source page '
                'for this exact model; a product dimension need not be visible in a photo. '
                'Remove a dimension only when its source/model or value is unsupported. '
                'Keep only downloaded image URLs already in '
                'the draft; you may discard an unhelpful image and renumber kept image IDs '
                'consecutively. Do not browse or call tools.'
            )
            _,audited,visual_usage,_=_call_codex(
                args,audit_prompt,out,phase,
                images=audit_images,visual=True)
            usage_records.append(visual_usage)
            _validate(audited)
            source_by_url={item['url']:out/item['path'] for item in report['images']}
            if any(item['url'] not in source_by_url for item in audited['images']):
                raise ValueError('visual audit introduced a new unverified image URL')
            selected=out/'selected_images'
            selected.mkdir(exist_ok=True)
            for stale in selected.iterdir():stale.unlink()
            for item in audited['images']:
                source=source_by_url[item['url']]
                dest=selected/(item['id']+source.suffix)
                shutil.copy2(source,dest)
                item['path']=str((Path('images')/dest.name))
            shutil.rmtree(images_dir)
            selected.rename(images_dir)
            report=audited
            (out/'evidence.json').write_text(json.dumps(report,indent=2))
            (out/'download_errors.txt').unlink(missing_ok=True)
            if not args.keep_audit:
                for path in out.glob('research_turn_*'):shutil.rmtree(path)
                (out/'schema.json').unlink()
                shutil.rmtree(out/'audit_images')
            fresh=sum(u.get('input_tokens',0)-u.get('cached_input_tokens',0)
                      for u in usage_records)
            cached=sum(u.get('cached_input_tokens',0) for u in usage_records)
            output=sum(u.get('output_tokens',0) for u in usage_records)
            return dict(status='complete',product=report['product'],evidence=str(out/'evidence.json'),
                        images=len(report['images']),parts=len(report['parts']),research_turns=len(usage_records),
                        web_search_calls=search_calls,fresh_input_tokens=fresh,
                        cached_input_tokens=cached,output_tokens=output)
        (out/'download_errors.txt').write_text('\n'.join(failures)+'\n')
    (out/'download_errors.txt').write_text('\n'.join(failures)+'\n')
    raise RuntimeError(f'could not download all selected images; inspect {out / "download_errors.txt"}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('product')
    parser.add_argument('--name',required=True)
    parser.add_argument('--output',help='Parent evidence directory; default BlenderAgent/research')
    parser.add_argument('--timeout',type=int,default=1800)
    parser.add_argument('--image-max-side',type=int,default=1260,
                        help='Maximum long side of images attached for the visual audit')
    parser.add_argument('--keep-audit',action='store_true')
    parser.add_argument('--resume',action='store_true',help='Repair/download from the last completed research response')
    parser.add_argument('--codex-bin',default=shutil.which('codex') or 'codex',help=argparse.SUPPRESS)
    args=parser.parse_args()
    try:print(json.dumps(run(args)))
    except Exception as exc:
        print('Research failed: '+str(exc),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
