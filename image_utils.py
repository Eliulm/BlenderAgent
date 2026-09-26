"""Prepare bounded-size image attachments without changing research originals."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

def downscale_images(images, output_dir, max_side=1260):
    """Return lossless PNG copies whose longest side is at most max_side pixels."""
    if max_side < 32:
        raise ValueError('image max side must be at least 32 pixels')
    sources=[Path(path).resolve() for path in images]
    output_dir=Path(output_dir).resolve()
    output_dir.mkdir(parents=True,exist_ok=True)
    outputs=[output_dir/f'image_{index:02d}.png'
             for index in range(1,len(sources)+1)]
    manifest=[{'source':str(source),'output':str(output)}
              for source,output in zip(sources,outputs)]
    script=(
        'import bpy, json\n'
        f'items=json.loads({json.dumps(json.dumps(manifest))})\n'
        f'limit={int(max_side)}\n'
        'for item in items:\n'
        '    image=bpy.data.images.load(item["source"],check_existing=False)\n'
        '    width,height=map(int,image.size)\n'
        '    if min(width,height)<1: raise ValueError("empty image: "+item["source"])\n'
        '    factor=min(1.0,limit/max(width,height))\n'
        '    if factor<1: image.scale(max(1,round(width*factor)),max(1,round(height*factor)))\n'
        '    image.filepath_raw=item["output"]\n'
        '    image.file_format="PNG"\n'
        '    image.save()\n'
        '    bpy.data.images.remove(image)\n'
    )
    blender=os.environ.get('BLENDER_BIN') or shutil.which('blender') or \
            '/Applications/Blender.app/Contents/MacOS/Blender'
    proc=subprocess.run([blender,'-b','--factory-startup','--python-exit-code','1',
                         '--python-expr',script],
                        capture_output=True,text=True,timeout=180,check=False)
    def valid(path):
        if not path.is_file():return False
        with path.open('rb') as stream:header=stream.read(24)
        if header[:8]!=b'\x89PNG\r\n\x1a\n' or len(header)<24:return False
        width=int.from_bytes(header[16:20],'big')
        height=int.from_bytes(header[20:24],'big')
        return 0<width<=max_side and 0<height<=max_side
    if proc.returncode or not all(map(valid,outputs)):
        raise RuntimeError('reference image resize failed: '+(proc.stderr or proc.stdout)[-3000:])
    return outputs
