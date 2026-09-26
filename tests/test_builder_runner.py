"""Exercise the code-only loop without paid model calls."""
import importlib.util
from argparse import Namespace
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory

path=Path(__file__).resolve().parents[1]/'builder'/'runner.py'
spec=importlib.util.spec_from_file_location('generic_builder_runner',path)
runner=importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

FAKE='''#!/usr/bin/env python3
import json,sys
from pathlib import Path
prompt=sys.stdin.read()
final=Path(sys.argv[sys.argv.index('-o')+1])
phase=int(final.parent.name)
auto=final.parents[3].name.startswith('auto')
if phase==0:
 assert any('reference_images/image_01.png' in arg for arg in sys.argv)
 response={'action':'write','python':'# version0\\ndef build(output_dir):\\n    pass\\n',
           'edits':[],'accept_if_verified':False}
elif phase==1:
 assert 'Current authoritative build.py:' not in prompt
 assert 'version0' not in prompt
 assert 'test overlap' in prompt
 assert '-i' in sys.argv
 response={'action':'edit','python':'','edits':[{'old':'version0','new':'version1'}],
           'accept_if_verified':auto}
else:
 assert 'Current authoritative build.py:' not in prompt
 assert 'version1' not in prompt
 response={'action':'accept','python':'','edits':[],'accept_if_verified':False}
final.write_text(json.dumps(response))
print(json.dumps({'type':'thread.started','thread_id':'fake-thread'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(response)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100*(phase+1),'cached_input_tokens':0,'output_tokens':20*(phase+1)}}))
'''

with TemporaryDirectory(prefix='blenderagent_runner_') as temp:
    root=Path(temp)
    image=root/'image.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic')
    evidence=root/'evidence.json'
    evidence.write_text(json.dumps({'product':'Synthetic test object','parts':[{'name':'body'}],
                                    'images':[{'path':'image.png','description':'test front view'}]}))
    codex=root/'fake_codex.py'
    codex.write_text(FAKE)
    codex.chmod(codex.stat().st_mode|stat.S_IXUSR)
    actual_verify=runner._verify
    actual_downscale=runner.downscale_images
    def fake_downscale(images,out,max_side):
        assert max_side==1260
        out.mkdir(parents=True,exist_ok=True)
        prepared=out/'image_01.png'
        prepared.write_bytes(Path(images[0]).read_bytes())
        return [prepared]
    runner.downscale_images=fake_downscale
    def fake_verify(recipe,attempt,args):
        assert recipe.is_file()
        (attempt/'preview.png').write_bytes(image.read_bytes())
        if 'version0' in recipe.read_text():
            result={'ok':False,'geometry_ok':False,'errors':['test overlap'],
                    'preview':str(attempt/'preview.png'),'preview_status':'ready'}
            (attempt/'result.json').write_text(json.dumps(result))
            return result
        assert 'version1' in recipe.read_text()
        (attempt/'asset.blend').write_bytes(b'blend')
        parts=attempt/'parts'
        parts.mkdir(exist_ok=True)
        obj=parts/'body__steel.obj'
        obj.write_text('o body\n')
        result={'ok':True,'geometry_ok':True,'preview':str(attempt/'preview.png'),
                'preview_status':'ready','asset':str(attempt/'asset.blend'),'objs':[str(obj)]}
        (attempt/'result.json').write_text(json.dumps(result))
        return result
    runner._verify=fake_verify
    args=Namespace(evidence=str(evidence),name='test',output=str(root/'output'),
                   model='gpt-6-astra',effort='low',width=600,samples=2,
                   reference_max_side=1260,
                   blender_timeout=30,model_timeout=30,keep_work=False,
                   resume=False,codex_bin=str(codex))
    result=runner.run(args)
    assert result['model_turns']==3 and result['verifications']==2 and result['parts']==1
    assert result['auto_accepted'] is False
    out=Path(result['output'])
    assert sorted(str(p.relative_to(out)) for p in out.rglob('*') if p.is_file())==[
        'asset.blend','parts/body__steel.obj']
    args.name='resumed'
    interrupted=[False]
    def interrupted_verify(recipe,attempt,args):
        if not interrupted[0]:
            interrupted[0]=True
            raise KeyboardInterrupt()
        return fake_verify(recipe,attempt,args)
    runner._verify=interrupted_verify
    try:runner.run(args)
    except KeyboardInterrupt:pass
    else:raise AssertionError('test did not interrupt the verifier')
    state=json.loads((root/'output/resumed/.work/state.json').read_text())
    assert state['status']=='verifying' and len(state['turns'])==1
    args.resume=True
    resumed=runner.run(args)
    assert resumed['model_turns']==3 and resumed['verifications']==2
    assert not (Path(resumed['output'])/'.work').exists()
    args.name='auto'
    args.resume=False
    auto_result=runner.run(args)
    assert auto_result['model_turns']==2 and auto_result['verifications']==2
    assert auto_result['auto_accepted'] is True and auto_result['parts']==1
    args.name='auto_resume'
    original_publish=runner._publish
    interrupted_publish=[False]
    def publish_once(result,out):
        if not interrupted_publish[0]:
            interrupted_publish[0]=True
            raise KeyboardInterrupt()
        return original_publish(result,out)
    runner._publish=publish_once
    try:runner.run(args)
    except KeyboardInterrupt:pass
    else:raise AssertionError('test did not interrupt automatic publication')
    state=json.loads((root/'output/auto_resume/.work/state.json').read_text())
    assert state['status']=='awaiting_visual_review' and len(state['turns'])==2
    args.resume=True
    auto_resumed=runner.run(args)
    assert auto_resumed['model_turns']==2 and auto_resumed['auto_accepted'] is True
    runner._publish=original_publish
    runner._verify=actual_verify
    runner.downscale_images=actual_downscale
    print('PASS: code-only build, compact follow-up, conditional acceptance, and resume after interruption')

try:runner._apply({'action':'edit','python':'','edits':[{'old':'x','new':'y'}],
                   'accept_if_verified':False},Path(__file__),False,False)
except ValueError as exc:assert 'matched' in str(exc)
else:raise AssertionError('bad exact edit was accepted')
print('PASS: unmatched source edit is rejected')

with TemporaryDirectory(prefix='blenderagent_decline_') as temp:
    try:runner._apply({'action':'write','python':'# I cannot build the required object.\n',
                       'edits':[],'accept_if_verified':False},Path(temp)/'build.py',True,False)
    except ValueError as exc:assert 'did not define build()' in str(exc)
    else:raise AssertionError('comment-only response was sent to Blender')
print('PASS: comment-only refusal stops before Blender verification')

assert runner._tool_calls([
    {'type':'item.completed','item':{'type':'error','message':'HTTPS transport fallback'}},
    {'type':'item.completed','item':{'type':'agent_message','text':'accepted'}}])==[]
assert runner._tool_calls([
    {'type':'item.started','item':{'type':'command_execution'}}])==['command_execution']
print('PASS: transport diagnostics are not mistaken for model tool calls')
