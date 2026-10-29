"""Use GPT-6 Luna to research a named object and download a compact evidence set.

python researcher/runner.py 'Exact product name' --name output_slug
"""
from __future__ import annotations

import argparse
import copy
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
AUDIT_SCHEMA={
    'type':'object','additionalProperties':False,
    'properties':{
        'report':SCHEMA,
        'needs_research':{'type':'boolean'},
        'issues':{'type':'array','items':{'type':'string'}},
    },
    'required':['report','needs_research','issues'],
}
SCREEN_SCHEMA={
    'type':'object','additionalProperties':False,
    'properties':{'images':{'type':'array','items':{
        'type':'object','additionalProperties':False,
        'properties':{
            'id':{'type':'string'},'useful':{'type':'boolean'},
            'kind':{'type':'string','enum':['exterior','interior','dimension','irrelevant']},
            'observed':{'type':'string'},'reason':{'type':'string'},
        },
        'required':['id','useful','kind','observed','reason'],
    }}},
    'required':['images'],
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
        name=item.get('name','').lower()
        reason=item.get('reason','').lower()
        if re.search(r'\boptional\b',name) or re.search(
                r'include only if|if modeling the|if modelling the',reason):
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


def _repair_audit(audited,draft):
    """Restore each audited image's draft path/URL pair and renumber IDs consecutively.

    The audit only selects and re-describes already downloaded images, and files are
    re-copied under their final IDs afterwards, so these slips need no new research turn.
    """
    result=copy.deepcopy(audited)
    url_by_path={item['path']:item['url'] for item in draft['images']}
    path_by_url={item['url']:item['path'] for item in draft['images']}
    images=[]
    for item in result['images']:
        if item.get('path') in url_by_path:item['url']=url_by_path[item['path']]
        elif item.get('url') in path_by_url:item['path']=path_by_url[item['url']]
        else:continue
        if all(item['path']!=kept['path'] for kept in images):images.append(item)
    mapping={}
    for index,item in enumerate(images,1):
        mapping.setdefault(item.get('id'),f'image_{index:02d}')
        item['id']=f'image_{index:02d}'
    result['images']=images
    for part in result['parts']:
        part['evidence']=list(dict.fromkeys(mapping.get(ref,ref) for ref in part['evidence']
                                            if not re.fullmatch(r'image_\d+',ref) or ref in mapping))
    return result


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


def _search_queries(out):
    queries=[]
    for path in sorted(out.glob('research_turn_*/events.jsonl')):
        for line in path.read_text().splitlines():
            try:event=json.loads(line)
            except json.JSONDecodeError:continue
            action=event.get('item',{}).get('action',{})
            if isinstance(action,dict) and action.get('type')=='search':
                queries.extend(action.get('queries',[]))
    return list(dict.fromkeys(queries))


def _prune_report(report,keep_ids,aliases=None):
    result=copy.deepcopy(report)
    aliases=aliases or {}
    old_ids={item['id'] for item in report['images']}
    mapping={item['id']:f'image_{index:02d}' for index,item in
             enumerate((item for item in report['images'] if item['id'] in keep_ids),1)}
    result['images']=[dict(item,id=mapping[item['id']]) for item in result['images']
                      if item['id'] in mapping]
    for part in result['parts']:
        part['evidence']=list(dict.fromkeys(
            mapping.get(aliases.get(ref,ref),ref) for ref in part['evidence']
            if ref not in old_ids or ref in keep_ids or ref in aliases))
    return result


def _deduplicate_urls(report,paths,keep_ids):
    by_url={}
    aliases={}
    for item,path in zip(report['images'],paths):
        if item['id'] not in keep_ids:continue
        url=item['url']
        if url not in by_url:
            by_url[url]=(item['id'],path.stat().st_size)
        elif path.stat().st_size>by_url[url][1]:
            old_winner=by_url[url][0]
            for old_id,winner in list(aliases.items()):
                if winner==old_winner:aliases[old_id]=item['id']
            aliases[old_winner]=item['id']
            by_url[url]=(item['id'],path.stat().st_size)
        else:
            aliases[item['id']]=by_url[url][0]
    return {winner for winner,_ in by_url.values()},aliases


def _screen_prompt(product):
    return (
        'Look only at the ATTACHED images, in image_01, image_02, etc. order. '
        'The named product is '+product+'. No draft captions are provided: '
        'describe what the pixels actually show in each image. Classify its '
        'actual pixels as exterior, interior, dimension, or irrelevant. For each image, '
        'set useful=false if it is blank, depicts the wrong object, or shows '
        'the product too small or obscured by a person, packaging, text, or '
        'scenery to reveal useful modeling geometry. A close-up is useful '
        'when it clearly shows an important feature. A teardown of a closely '
        'related model is useful when it shows a plausible assembly analogue, '
        'even if its exterior differs. Judge each image independently. Return '
        'exactly one observation per attachment, in order. Do not browse or '
        'call tools.'
    )


def _call_codex(args,prompt,out,phase,thread=None,images=(),visual=False,schema_path=None):
    path=out/f'research_turn_{phase:02d}'
    path.mkdir()
    (path/'prompt.txt').write_text(prompt)
    final=path/'response.json'
    base=['-m','gpt-6-luna','-c','model_reasoning_effort="low"',
          '-c',f'web_search="{"disabled" if visual else "live"}"',
          '-c',f'sandbox_mode="{"read-only" if visual else "workspace-write"}"',
          '-c','approval_policy="never"',
          '--skip-git-repo-check','--json','--output-schema',str(schema_path or out/'schema.json'),
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
    (out/'audit_schema.json').write_text(json.dumps(AUDIT_SCHEMA,indent=2))
    (out/'screen_schema.json').write_text(json.dumps(SCREEN_SCHEMA,indent=2))
    report=None
    failures=[]
    usage_records=[]
    search_calls=0
    search_turns=0
    phase=0
    if args.resume:
        turns=sorted(out.glob('research_turn_*'))
        completed=[path for path in turns if (path/'response.json').is_file()]
        if not completed:
            raise ValueError('no completed research response to resume')
        last_response=json.loads((completed[-1]/'response.json').read_text())
        search_responses=[response for path in completed
                          if (response:=json.loads((path/'response.json').read_text())).get('product')]
        if not search_responses:
            raise ValueError('no completed search response to resume')
        report=search_responses[-1]
        if 'needs_research' in last_response:
            failures=(last_response['issues'] or ['visual audit requested more relevant images']) \
                if last_response['needs_research'] else []
        else:
            failures=(out/'download_errors.txt').read_text().splitlines() \
                if (out/'download_errors.txt').is_file() else []
        phase=max(int(path.name.rsplit('_',1)[-1]) for path in turns)+1
        for path in completed:
            events=[]
            for line in (path/'events.jsonl').read_text().splitlines():
                try:events.append(json.loads(line))
                except json.JSONDecodeError:pass
            usage_records.append(next((e.get('usage',{}) for e in reversed(events)
                                       if e.get('type')=='turn.completed'),{}))
            search_calls+=sum(e.get('type')=='item.completed' and
                              e.get('item',{}).get('type')=='web_search' for e in events)
            if json.loads((path/'response.json').read_text()).get('product'):
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
                    'research and already valid local images. Address every listed failure. '
                    'For visual relevance or coverage failures, search again and replace '
                    'weak images with useful evidence. For missing or corrupt files, '
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
            screen_images=downscale_images(downloaded,out/'screen_images',args.image_max_side)
            _,screen,screen_usage,_=_call_codex(
                args,_screen_prompt(report['product']),out,phase,
                images=screen_images,visual=True,
                schema_path=out/'screen_schema.json')
            usage_records.append(screen_usage)
            phase+=1
            observations=screen['images']
            if [item['id'] for item in observations]!=[item['id'] for item in report['images']]:
                raise ValueError('image screen returned wrong image IDs or order')
            keep_ids={item['id'] for item in observations
                      if item['useful'] and item['kind']!='irrelevant'}
            keep_ids,aliases=_deduplicate_urls(report,downloaded,keep_ids)
            draft=_prune_report(report,keep_ids,aliases)
            observation_by_id={item['id']:item for item in observations}
            old_id_by_path={item['path']:item['id'] for item in report['images']}
            for item in draft['images']:
                observed=observation_by_id[old_id_by_path[item['path']]]
                item['kind']=observed['kind']
                item['description']=observed['observed']
            if not draft['images'] or not any(item['kind']=='exterior' for item in draft['images']):
                failures=['image screen found no useful full-product exterior view: '+
                          '; '.join(item['id']+' '+item['reason'] for item in observations
                                    if not item['useful'])]
                (out/'download_errors.txt').write_text('\n'.join(failures)+'\n')
                continue
            kept_paths=[path for item,path in zip(report['images'],downloaded)
                        if item['id'] in keep_ids]
            audit_images=downscale_images(kept_paths,out/'audit_images',args.image_max_side)
            audit_prompt=(
                'Visually audit the ATTACHED downloaded images in their image_01, image_02, '
                'etc. order. Attachments are bounded-size copies of the saved originals. '
                'Here are the searches actually performed:\n'+
                json.dumps(_search_queries(out)[-40:])+
                '\nAn image-only screen, performed without seeing draft captions, observed:\n'+
                json.dumps(observations)+
                '\nHere is the draft research evidence after removing images that the '
                'screen found unusable:\n'+json.dumps(draft,indent=2)+
                '\nReturn an audit object with report, needs_research, and issues. Inspect '
                'EVERY image\'s actual pixels. An image must reveal a useful shape, surface, '
                'material boundary, scale, or assembly relationship of the named product. '
                'Reject images dominated by a person, unrelated object, package, text, '
                'or scenery when the target is too small or obscured to model from. Reject '
                'near-duplicate views that add no geometry. Do not rescue an irrelevant '
                'photo by writing a favorable description. Correct every view description '
                'and part inference; do not cite an image for internals it does not show. '
                'The draft image kinds and descriptions now come from an image-only '
                'screen; preserve those observed facts. Keep each image path and URL '
                'together. If the pixels clearly conflict with their claimed source '
                'or model, set needs_research=true and request a corrected mapping. '
                'For a complex enclosed assembly, use the actual search queries to judge '
                'whether exact-model and related-model internals were sought. Retain a '
                'useful related-model teardown as a labeled visual analogue, even if its '
                'exterior differs; never treat it as proof of exact unseen geometry. '
                'Set needs_research=true only when no usable full-object exterior view '
                'remains, most selected images are irrelevant, or a complex enclosed '
                'assembly has no useful internal view and the search stopped at the '
                'exact model without trying closely related models. Do not request more research '
                'merely for missing rear/underside angles, exact dimensions, or an exact '
                'teardown after reasonable searches; record those limitations as gaps. '
                'A missing interior image is acceptable when exact and related-model '
                'searches were tried without a useful result. When needs_research=true, keep '
                'the original image IDs, URLs, and paths in report so the search turn '
                'can replace them. When needs_research=false, discard unhelpful images '
                'and renumber retained IDs consecutively. '
                'Keep necessary batteries, motors, coils and circuits in the required '
                'parts list for enclosed electric products even when no teardown '
                'images are available; mark unsupported shapes inferred. The part '
                'list must be structurally complete (no major part missing that holds '
                'the assembly together; no screws or minor hardware) and include every '
                'exterior part plus electromagnetically relevant internals. Parts fully '
                'enclosed by a closed conductive shell without significant openings '
                'are EM-invisible; remove them unless structurally needed. '
                'Prefer retaining images that add a new viewpoint. '
                'Distinguish an optional removable accessory from an installed '
                'replaceable component essential to normal use. The parts array '
                'must contain only required physical regions of the bare product; merge '
                'same-material continuous features, but keep distinct major internal '
                'components and material regions as separate requirements. '
                'Correct unsupported dimensional claims '
                'rather than inventing values. Preserve measurements cited to a source page '
                'for this exact model; a product dimension need not be visible in a photo. '
                'Remove a dimension only when its source/model or value is unsupported. '
                'Keep only downloaded image paths and URLs already in the draft. Do not browse '
                'or call tools.'
            )
            _,audit,visual_usage,_=_call_codex(
                args,audit_prompt,out,phase,
                images=audit_images,visual=True,schema_path=out/'audit_schema.json')
            usage_records.append(visual_usage)
            phase+=1
            audited=audit['report']
            if audit['needs_research']:
                failures=audit['issues'] or ['visual audit requested more relevant images']
                (out/'download_errors.txt').write_text('\n'.join(failures)+'\n')
                continue
            audited=_repair_audit(audited,draft)
            _validate(audited)
            source_by_path={item['path']:out/item['path'] for item in report['images']
                            if item['id'] in keep_ids}
            allowed_pairs={(item['path'],item['url']) for item in draft['images']}
            if any((item['path'],item['url']) not in allowed_pairs
                   for item in audited['images']):
                raise ValueError('visual audit changed a downloaded image path or URL')
            selected=out/'selected_images'
            selected.mkdir(exist_ok=True)
            for stale in selected.iterdir():stale.unlink()
            for item in audited['images']:
                source=source_by_path[item['path']]
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
                (out/'audit_schema.json').unlink()
                (out/'screen_schema.json').unlink()
                shutil.rmtree(out/'screen_images')
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
    raise RuntimeError(f'could not assemble relevant visual evidence; inspect {out / "download_errors.txt"}')


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
