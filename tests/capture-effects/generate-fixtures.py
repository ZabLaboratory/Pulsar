from PIL import Image
from pathlib import Path
root=Path('tests/capture-effects/fixtures')
for name,transparent in [('foreground',True),('background',False)]:
    im=Image.new('RGBA',(320,180))
    for y in range(180):
        for x in range(320):
            if transparent:
                color=(255,255,255,255) if (x//8+y//8)%2 else (20,40,220,255)
                if x>160: color=(*color[:3],90)
            else: color=(220,30,30,255) if (x//8+y//8)%2 else (30,220,30,255)
            im.putpixel((x,y),color)
    im.save(root/(name+'.png'))
