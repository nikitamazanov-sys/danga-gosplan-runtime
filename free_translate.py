"""Offline English -> Russian translation. No paid API or runtime downloads."""
import json
import os
import re
import threading
from pathlib import Path

MODEL=Path(os.environ.get('INSPIRATION_TRANSLATION_MODEL','/opt/inspiration-enru'))
_engine=None
_load=threading.Lock()

def validate(raw):
    if not isinstance(raw,dict) or set(raw)!={'texts'}:raise ValueError('invalid_translation_payload')
    texts=raw['texts']
    if not isinstance(texts,list) or not 1<=len(texts)<=64:raise ValueError('invalid_translation_payload')
    if any(not isinstance(t,str) or not t.strip() or len(t)>6000 for t in texts):raise ValueError('invalid_translation_payload')
    if sum(map(len,texts))>6000:raise ValueError('translation_payload_too_large')
    return texts

def chunks(text,encode,limit=256):
    result=[]
    for sentence in re.split(r'(?<=[.!?])\s+|\n+',text.strip()):
        if not sentence:continue
        words=sentence.split();current=[]
        for word in words:
            candidate=' '.join(current+[word])
            if len(encode(candidate))>limit:
                if current:result.append(encode(' '.join(current)));current=[]
                if len(encode(word))>limit:raise ValueError('unbroken_translation_token')
            current.append(word)
        if current:result.append(encode(' '.join(current)))
    if not result:raise ValueError('empty_translation_input')
    return result

def engine():
    global _engine
    with _load:
        if _engine is None:
            import ctranslate2
            import sentencepiece as spm
            source=spm.SentencePieceProcessor(model_file=str(MODEL/'source.spm'))
            target=spm.SentencePieceProcessor(model_file=str(MODEL/'target.spm'))
            translator=ctranslate2.Translator(str(MODEL),device='cpu',compute_type='int8',inter_threads=1,intra_threads=1)
            _engine=(translator,source,target)
    return _engine

def translate(texts):
    translator,source,target=engine()
    batches=[];owners=[]
    for index,text in enumerate(texts):
        for piece in chunks(text,lambda t:source.encode(t,out_type=str)):
            batches.append(piece);owners.append(index)
    results=translator.translate_batch(batches,beam_size=2,max_batch_size=1024,batch_type='tokens',max_input_length=0,max_decoding_length=768)
    output=[[] for _ in texts]
    if len(results)!=len(batches):raise ValueError('incomplete_translation')
    for owner,result in zip(owners,results):
        tokens=result.hypotheses[0]
        if len(tokens)>=768:raise ValueError('truncated_translation')
        decoded=target.decode(tokens).strip()
        if not decoded:raise ValueError('empty_translation')
        output[owner].append(decoded)
    return [' '.join(parts) for parts in output]
