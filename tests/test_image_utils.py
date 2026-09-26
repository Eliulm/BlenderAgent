"""Check the actual Blender-backed attachment resize on a tiny synthetic PNG."""
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import zlib

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from image_utils import downscale_images


def chunk(kind,data):
    return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))


with TemporaryDirectory(prefix='blenderagent_resize_') as temp:
    root=Path(temp)
    source=root/'original.png'
    width,height=64,32
    rows=b''.join(b'\0'+bytes((255,20,10))*width for _ in range(height))
    png=(b'\x89PNG\r\n\x1a\n'+
         chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+
         chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))
    source.write_bytes(png)
    result=downscale_images([source],root/'attachments',32)
    assert len(result)==1
    assert struct.unpack('>II',result[0].read_bytes()[16:24])==(32,16)
    assert source.read_bytes()==png
print('PASS: reference attachment downscaled; source unchanged')
