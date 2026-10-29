"""Run a researched object through code-only Sol -> Blender -> visual feedback.

python builder/runner.py --evidence research/product/evidence.json --name product
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
from string import Template
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from image_utils import downscale_images
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'action': {'type':'string','enum':['write','edit','accept']},
        'python': {'type':'string'},
        'edits': {'type':'array','items':{'type':'object','additionalProperties':False,
                   'properties':{'old':{'type':'string'},'new':{'type':'string'}},
                   'required':['old','new']}},
        'accept_if_verified': {'type':'boolean'},
    },
    'required':['action','python','edits','accept_if_verified'],
}
FOLLOWUP = """Verification attempt {number} is finished. Result: {result}
{preview_note}
{source_note}
Before choosing code, think through two independent reviews internally:
1. Verification: diagnose each error and choose the specific source changes
   that would repair it.
2. Visual: compare the render with the original reference attachments,
   allowing only for the deliberate variations in your assumptions. First
   assess the overall silhouette, main proportions, assembly placement and
   large openings in every relevant view; only then inspect smaller surface
   details. Identify the largest concrete mismatch, its reference image/view,
   and the shape change that would correct it. Before declaring the exterior
   acceptable, compare at least two visible landmark relationships (such as
   part length ratios, angles, opening size, or curvature) in matched views.
   Mere category recognition is insufficient.
   Minor grooves or texture do not count as refinement while a larger shape
   mismatch remains.
Then combine both plans into one coherent edit. Make the code implement both
in this turn when possible. Do not defer visual changes just because
verification failed. If no useful preview exists, focus on repair. Do not
output the reviews or a plan. Return only the structured response: edit exact
unique old/new source text, write a complete replacement, or accept only if
geometry passed and the image is acceptable. Set accept_if_verified=true on
an edit only when the previous
render already matched the major exterior and the edit is unlikely to change
its appearance. The runner applies, verifies, and renders every code change;
a passing edit with that flag is published without another model turn.
Do not spend a turn changing only a presentation camera unless existing views
hide a required feature and prevent judging the physical asset. If a verifier
times out on dense remeshing, switch to a cheaper local geometry repair.
"""


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _save(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(data,indent=2))
    temp.replace(path)


def _evidence(path):
    data = json.loads(path.read_text())
    if not isinstance(data.get('product'),str) or not data['product']:
        raise ValueError('evidence needs a product name')
    if not isinstance(data.get('parts'),list) or not data['parts']:
        raise ValueError('evidence needs a nonempty required-parts list')
    images = data.get('images',[])
    if not isinstance(images,list) or not 1 <= len(images) <= 10:
        raise ValueError('evidence needs 1–10 images')
    resolved = []
    for i,item in enumerate(images,1):
        image_path = (path.parent/item['path']).resolve()
        if not image_path.is_file() or not item.get('description'):
            raise ValueError(f'image {i} missing file or description')
        resolved.append(image_path)
    return data,resolved


def _tool_calls(events):
    found=[]
    for event in events:
        if event.get('type') not in ('item.started','item.completed'):
            continue
        item=event.get('item',{})
        if item.get('type') not in ('agent_message','reasoning','error'):
            found.append(item.get('type'))
    return list(dict.fromkeys(found))


def _invoke(cmd,prompt,work,phase,timeout,schema):
    turn=work/'turns'/f'{phase:02d}'
    final=turn/'final.json'
    if turn.exists():
        if not final.is_file():
            raise RuntimeError(f'model turn {phase} was interrupted before a final response; '
                               f'inspect {turn} before retrying it')
    else:
        turn.mkdir(parents=True)
        (turn/'prompt.txt').write_text(prompt)
        cmd=cmd[:-1]+['--output-schema',str(schema),'-o',str(final),cmd[-1]]
        with (turn/'prompt.txt').open('rb') as stdin, (turn/'events.jsonl').open('wb') as stdout, \
                (turn/'stderr.log').open('wb') as stderr:
            proc=subprocess.run(cmd,cwd=ROOT,stdin=stdin,stdout=stdout,stderr=stderr,
                                timeout=timeout,check=False)
        if proc.returncode:
            raise RuntimeError(f'model turn {phase} exited {proc.returncode}; see {turn / "stderr.log"}')
    events=[]
    for line in (turn/'events.jsonl').read_text().splitlines():
        try:events.append(json.loads(line))
        except json.JSONDecodeError:pass
    calls=_tool_calls(events)
    if calls:
        raise RuntimeError('builder called tools in code-only mode: '+repr(calls))
    thread=next((e['thread_id'] for e in events if e.get('type')=='thread.started'),None)
    complete=next((e for e in reversed(events) if e.get('type')=='turn.completed'),None)
    if not thread or not complete or not final.is_file():
        raise RuntimeError(f'incomplete model turn {phase}; see {turn}')
    return thread,complete.get('usage',{}),json.loads(final.read_text())


def _apply(response,recipe,first,prior_ok):
    if not isinstance(response,dict) or set(response)!=set(SCHEMA['required']):
        raise ValueError('response needs exactly action, python, edits, accept_if_verified')
    action,code,edits=response['action'],response['python'],response['edits']
    if not isinstance(code,str) or not isinstance(edits,list) or \
            not isinstance(response['accept_if_verified'],bool):
        raise ValueError('invalid code response fields')
    if action=='accept':
        if first or not prior_ok or code or edits or response['accept_if_verified']:
            raise ValueError('accept requires a passing previous verification and no code')
        return action
    if action=='write':
        if not code.strip() or edits:
            raise ValueError('write requires nonempty complete code and no edits')
        try:
            parsed=ast.parse(code)
        except SyntaxError:
            parsed=None  # Let Blender report syntax errors so the model can repair them.
        if parsed is not None and not any(isinstance(node,ast.FunctionDef) and
                                           node.name=='build' for node in parsed.body):
            raise ValueError('write did not define build(); stopping before Blender verification')
        recipe.write_text(code)
        return action
    if action!='edit' or first or code or not edits:
        raise ValueError('invalid edit response')
    source=recipe.read_text()
    for index,edit in enumerate(edits,1):
        if not isinstance(edit,dict) or set(edit)!={'old','new'} or \
                not isinstance(edit['old'],str) or not isinstance(edit['new'],str) or not edit['old']:
            raise ValueError(f'invalid edit {index}')
        count=source.count(edit['old'])
        if count!=1:
            raise ValueError(f'edit {index} old text matched {count} times; expected one')
        source=source.replace(edit['old'],edit['new'],1)
    recipe.write_text(source)
    return action


def _compact(result):
    keys=('ok','geometry_ok','preview','preview_status','preview_error','preview_warnings',
          'preview_panels','errors','bounds_mm','radius_mm','parts','error')
    out={k:result[k] for k in keys if k in result}
    overlap=result.get('overlaps',[])
    if overlap:
        out['overlaps']=overlap[:20]
        if len(overlap)>20:out['additional_overlaps']=len(overlap)-20
    if isinstance(out.get('error'),str) and len(out['error'])>6000:
        out['error']=out['error'][-6000:]
    return out


def _verify(recipe,attempt,args):
    cmd=[sys.executable,str(HERE/'verify.py'),str(recipe),'--out',str(attempt),
         '--width',str(args.width),'--samples',str(args.samples),
         '--timeout',str(args.blender_timeout)]
    with (attempt/'verify.log').open('wb') as log:
        try:
            subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                           timeout=args.blender_timeout+30,check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'verifier exceeded timeout; see {attempt / "verify.log"}') from exc
    result=attempt/'result.json'
    if not result.is_file():
        raise RuntimeError(f'verifier returned no result; see {attempt / "verify.log"}')
    return json.loads(result.read_text())


def _publish(result,out):
    blend=Path(result['asset'])
    objs=[Path(p) for p in result['objs']]
    if not blend.is_file() or not objs or any(not p.is_file() for p in objs):
        raise RuntimeError('accepted verifier result lacks blend or OBJ files')
    shutil.copy2(blend,out/'asset.blend')
    parts=out/'parts'
    parts.mkdir(exist_ok=True)
    for path in objs:
        shutil.copy2(path,parts/path.name)
    return len(objs)


def _summary(state,data,out):
    usage=state['turns'][-1]['usage']
    return dict(status='complete',product=data['product'],output=str(out),
                parts=state['exported_parts'],model_turns=len(state['turns']),
                verifications=len(state['verifications']),
                auto_accepted=state.get('auto_accepted',False),
                fresh_input_tokens=usage.get('input_tokens',0)-usage.get('cached_input_tokens',0),
                cached_input_tokens=usage.get('cached_input_tokens',0),
                output_tokens=usage.get('output_tokens',0),usage=usage)


def _finish(state,data,out,work,result,keep_work,auto_accepted):
    state['exported_parts']=_publish(result,out)
    state['status']='complete'
    state['auto_accepted']=auto_accepted
    _save(work/'state.json',state)
    summary=_summary(state,data,out)
    if not keep_work:shutil.rmtree(work)
    return summary


def run(args):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',args.name):
        raise ValueError('--name must be a simple unique directory name')
    evidence_path=Path(args.evidence).expanduser().resolve()
    data,images=_evidence(evidence_path)
    out=(Path(args.output).expanduser().resolve() if args.output else ROOT/'output')/args.name
    work=out/'.work'
    recipe=work/'build.py'
    schema=work/'code.schema.json'
    identity=dict(product=data['product'],model=args.model,effort=args.effort,
                  reference_max_side=args.reference_max_side,
                  evidence_sha256=_sha(evidence_path),prompt_sha256=_sha(HERE/'prompt.txt'),
                  verifier_sha256=_sha(HERE/'verify.py'),
                  image_sha256=[_sha(p) for p in images])
    if args.resume:
        if not (work/'state.json').is_file():
            raise ValueError('no interrupted .work/state.json to resume')
        state=json.loads((work/'state.json').read_text())
        if any(state.get(key)!=value for key,value in identity.items()):
            raise ValueError('evidence, model, prompt, or verifier changed since the interrupted run')
        if state.get('status')=='complete':
            raise ValueError('this run is already complete')
    else:
        out.mkdir(parents=True,exist_ok=False)
        work.mkdir()
        _save(schema,SCHEMA)
        state=dict(**identity,status='starting',turns=[],verifications=[])
        _save(work/'state.json',state)
    prepared_images=downscale_images(images,work/'reference_images',args.reference_max_side)
    # Absolute paths in a run-specific prompt keep the static template portable.
    evidence_copy=dict(data)
    evidence_copy['images']=[dict(item,id=f'image_{i:02d}',path=str(path))
                             for i,(item,path) in enumerate(zip(data['images'],prepared_images),1)]
    prompt=Template((HERE/'prompt.txt').read_text()).substitute(
        VERIFY_PATH=HERE/'verify.py',RECIPE_PATH=recipe,BUILDER_DIR=HERE,
        EVIDENCE_JSON=json.dumps(evidence_copy,indent=2))
    thread=state.get('thread_id')
    phase=len(state['turns'])
    previous=None
    if phase:
        last=state['turns'][-1]
        if last.get('action')=='accept':
            accepted=work/'attempts'/f'{phase-2:02d}'/'result.json'
            if not accepted.is_file():
                raise RuntimeError('accepted candidate result missing during resume')
            candidate=json.loads(accepted.read_text())
            if not candidate.get('ok'):
                raise RuntimeError('last accepted candidate did not pass verification')
            return _finish(state,data,out,work,candidate,args.keep_work,False)
        if last.get('patch_error'):
            prior_preview=state['verifications'][-1].get('preview') if state['verifications'] else None
            previous=dict(ok=False,error=last['patch_error'],preview=prior_preview)
        else:
            attempt=work/'attempts'/f'{phase-1:02d}'
            result_path=attempt/'result.json'
            if result_path.is_file():
                previous=json.loads(result_path.read_text())
            else:
                if not attempt.is_dir():
                    attempt.mkdir(parents=True)
                previous=_verify(recipe,attempt,args)
            if not last.get('verification_complete'):
                state['verifications'].append(_compact(previous))
                last['verification_complete']=True
                state['status']='awaiting_visual_review' if previous.get('ok') else 'awaiting_repair'
                _save(work/'state.json',state)
            if phase>1 and last.get('accept_if_verified') and previous.get('ok'):
                return _finish(state,data,out,work,previous,args.keep_work,True)
    try:
        while True:
            base=['-m',args.model,'-c',f'model_reasoning_effort="{args.effort}"',
                  '-c','sandbox_mode="read-only"','--json','--skip-git-repo-check']
            if phase==0:
                model_prompt=prompt
                cmd=[args.codex_bin,'exec',*base,'-C',str(ROOT)]
                for image in prepared_images:
                    cmd += ['-i',str(image)]
                cmd += ['-']
            else:
                assert previous is not None and thread is not None
                image=Path(previous['preview']) if previous.get('preview') else None
                image=image if image and image.is_file() else None
                source_note=(
                    'The previous code response was applied exactly. The current recipe is '
                    'your original write plus your accepted edits; no other source changes '
                    'were made.'
                )
                if state['turns'][-1].get('patch_error'):
                    source_note=('The last edit was NOT applied. Current authoritative '
                                 'build.py:\n<python>\n'+recipe.read_text()+'\n</python>')
                model_prompt=FOLLOWUP.format(number=phase,result=json.dumps(_compact(previous)),
                                             preview_note='Preview attached.' if image else 'No preview was produced.',
                                             source_note=source_note)
                cmd=[args.codex_bin,'exec','resume',*base]
                if image:cmd += ['-i',str(image)]
                cmd += [thread,'-']
            old_sha=_sha(recipe) if recipe.is_file() else None
            thread,usage,response=_invoke(cmd,model_prompt,work,phase,args.model_timeout,schema)
            if (_sha(recipe) if recipe.is_file() else None)!=old_sha:
                raise RuntimeError('recipe changed during a code-only model turn')
            state['thread_id']=thread
            record=dict(phase=phase,action=response.get('action'),usage=usage,
                        accept_if_verified=response.get('accept_if_verified') if phase else False)
            state['turns'].append(record)
            try:
                action=_apply(response,recipe,phase==0,bool(previous and previous.get('ok')))
            except ValueError as exc:
                if phase==0 or not recipe.is_file():raise
                record['patch_error']=str(exc)
                previous=dict(ok=False,error='code response not applied: '+str(exc),
                              preview=previous.get('preview') if previous else None)
                state['status']='awaiting_repair'
                _save(work/'state.json',state)
                phase+=1
                continue
            record['recipe_sha256']=_sha(recipe)
            if action=='accept':
                return _finish(state,data,out,work,previous,args.keep_work,False)
            if phase and record['recipe_sha256']==old_sha:
                record['patch_error']='code response made no change'
                previous=dict(ok=False,error=record['patch_error'],
                              preview=previous.get('preview') if previous else None)
                state['status']='awaiting_repair'
                _save(work/'state.json',state)
                phase+=1
                continue
            attempt=work/'attempts'/f'{phase:02d}'
            attempt.mkdir(parents=True,exist_ok=True)
            state['status']='verifying'
            _save(work/'state.json',state)
            previous=_verify(recipe,attempt,args)
            state['verifications'].append(_compact(previous))
            record['verification_complete']=True
            state['status']='awaiting_visual_review' if previous.get('ok') else 'awaiting_repair'
            _save(work/'state.json',state)
            if phase and record['accept_if_verified'] and previous.get('ok'):
                return _finish(state,data,out,work,previous,args.keep_work,True)
            phase+=1
    except Exception as exc:
        state['status']='failed'
        state['error']=str(exc)
        _save(work/'state.json',state)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence',required=True)
    parser.add_argument('--name',required=True)
    parser.add_argument('--output',help='Parent output directory; default BlenderAgent/output')
    parser.add_argument('--model',default='gpt-6.1-sol')
    parser.add_argument('--effort',choices=('low','medium','high','xhigh'),default='low')
    parser.add_argument('--width',type=int,default=1440)
    parser.add_argument('--reference-max-side',type=int,default=1260,
                        help='Maximum long side of each reference image attached to Sol')
    parser.add_argument('--samples',type=int,default=16)
    parser.add_argument('--blender-timeout',type=int,default=600)
    parser.add_argument('--model-timeout',type=int,default=3600)
    parser.add_argument('--keep-work',action='store_true',help='Retain prompts, attempts, and cost audit')
    parser.add_argument('--resume',action='store_true',help='Continue a checkpointed interrupted run')
    parser.add_argument('--codex-bin',default=shutil.which('codex') or 'codex',help=argparse.SUPPRESS)
    args=parser.parse_args()
    try:print(json.dumps(run(args)))
    except Exception as exc:
        print('Builder failed: '+str(exc),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
