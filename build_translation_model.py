"""Convert the original Helsinki-NLP OPUS release at image build time only."""
import json,shutil,urllib.request,zipfile
from pathlib import Path
from ctranslate2.converters import OpusMTConverter

base=Path('/tmp/inspiration-model-build');base.mkdir()
archive=base/'model.zip'
url='https://object.pouta.csc.fi/OPUS-MT-models/en-ru/opus-2020-02-11.zip'
with urllib.request.urlopen(url,timeout=120) as response,archive.open('wb') as out:
    shutil.copyfileobj(response,out)
original=base/'original';original.mkdir()
with zipfile.ZipFile(archive) as z:
    if sum(x.file_size for x in z.infolist())>1500*1024*1024:raise RuntimeError('Model archive is too large')
    for item in z.infolist():
        path=Path(item.filename)
        if path.is_absolute() or '..' in path.parts:raise RuntimeError('Unsafe model archive')
    z.extractall(original)
models=list(original.rglob('source.spm'))
if len(models)!=1:raise RuntimeError('Unexpected model layout')
source=models[0].parent;destination=Path('/opt/inspiration-enru')
OpusMTConverter(str(source)).convert(str(destination),quantization='int8')
for name in ['source.spm','target.spm']:
    shutil.copy2(source/name,destination/name)
(destination/'NOTICE.txt').write_text('Helsinki-NLP OPUS-MT en-ru, opus-2020-02-11. Model card and license: https://huggingface.co/Helsinki-NLP/opus-mt-en-ru (Apache-2.0). CTranslate2: https://opennmt.net/CTranslate2/.\n')
shutil.rmtree(base)
