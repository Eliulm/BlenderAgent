import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory

path=Path(__file__).resolve().parents[1]/'researcher'/'runner.py'
spec=importlib.util.spec_from_file_location('generic_research_runner',path)
research=importlib.util.module_from_spec(spec)
spec.loader.exec_module(research)

base={'product':'Test object','complexity':'simple','internal_evidence_status':'not_needed',
      'images':[{'id':'image_01','url':'https://example.com/front.jpg',
                 'source_url':'https://example.com/product','path':'images/image_01.jpg',
                 'kind':'exterior',
                 'description':'front exterior view'}],
      'parts':[{'name':'body','material':'polymer','location':'outer',
                'reason':'main exterior','basis':'observed','evidence':['image_01']}],
      'dimensions':[],'notes':[],'gaps':[]}
research._validate(base)
assert research._image_type(b'\xff\xd8\xffxxx')=='.jpg'
assert research._image_type(b'\x89PNG\r\n\x1a\nxxx')=='.png'
assert research._image_type(b'RIFFxxxxWEBP')=='.webp'
optional=dict(base,parts=[dict(base['parts'][0],name='optional sheath')])
try:research._validate(optional)
except ValueError as exc:assert 'conditional part' in str(exc)
else:raise AssertionError('optional part accepted as required')

with TemporaryDirectory(prefix='blenderagent_research_') as temp:
    out=Path(temp)
    (out/'images').mkdir()
    image=out/'images/image_01.jpg'
    image.write_bytes(b'\xff\xd8\xff'+b'x'*40)
    paths,failures=research._local_images(base,out)
    assert paths==[image.resolve()] and not failures
    changed=dict(base,images=[dict(base['images'][0],url='https://example.com/new.jpg')])
    _,failures=research._local_images(changed,out,base,image.stat().st_mtime_ns+1)
    assert 'not replaced' in failures[0]
    image.write_bytes(b'<html>'+b'x'*40)
    _,failures=research._local_images(base,out)
    assert 'not a valid' in failures[0]
    escaped=dict(base,images=[dict(base['images'][0],path='../image_01.jpg')])
    _,failures=research._local_images(escaped,out)
    assert 'path must be' in failures[0]
print('PASS: Luna-saved image paths, bytes, and required-only evidence contract')
