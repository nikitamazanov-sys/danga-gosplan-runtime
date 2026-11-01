import io,json,os,unittest
from unittest.mock import patch
import free_translate as mt
import worker

class TranslationTests(unittest.TestCase):
    def test_chunks_preserve_all_words_without_silent_truncation(self):
        text=' '.join('word'+str(i) for i in range(1000))
        pieces=mt.chunks(text,str.split,limit=40)
        self.assertEqual([x for p in pieces for x in p],text.split())
        self.assertTrue(all(len(p)<=40 for p in pieces))

    def test_payload_has_no_urls_or_model_controls(self):
        for payload in ({'texts':[]},{'texts':['x'],'url':'https://example.com'},{'texts':['x'*6001]},{'texts':[None]}):
            with self.assertRaises(ValueError):mt.validate(payload)
        self.assertEqual(mt.validate({'texts':['Design exhibition']}),['Design exhibition'])

    def call(self,body,signed=True):
        raw=json.dumps(body).encode();status=[]
        env={'PATH_INFO':'/v1/translate','REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),'wsgi.input':io.BytesIO(raw)}
        def auth(*args,**kwargs):
            if not signed:raise worker.Failure(401,'invalid_signature')
        with patch.object(worker,'settings',return_value=('sk-test','s'*64,'model')),patch.object(worker,'authenticate',side_effect=auth),patch.object(mt,'translate',return_value=['Выставка дизайна']) as translate,patch.object(worker,'request_openai') as paid:
            result=b''.join(worker.application(env,lambda s,h:status.append(s)))
        return status[0],json.loads(result),translate,paid

    def test_translation_is_signed_and_never_calls_paid_api(self):
        status,result,translate,paid=self.call({'texts':['Design exhibition']})
        self.assertTrue(status.startswith('200'));self.assertEqual(result['texts'],['Выставка дизайна']);paid.assert_not_called()

    def test_unsigned_translation_is_rejected_before_inference(self):
        status,result,translate,paid=self.call({'texts':['Design exhibition']},signed=False)
        self.assertTrue(status.startswith('401'));translate.assert_not_called();paid.assert_not_called()

    def test_drawing_job_has_priority(self):
        with patch.dict(worker._jobs,{'a':{'status':'running'}},clear=True):
            status,result,translate,paid=self.call({'texts':['Design exhibition']})
        self.assertTrue(status.startswith('503'));translate.assert_not_called();paid.assert_not_called()

if __name__=='__main__':unittest.main()
