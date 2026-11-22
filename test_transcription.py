import io,json,base64,unittest
from unittest.mock import patch
import worker
class TranscriptionTests(unittest.TestCase):
 def body(self,mime='audio/wav',data=None):return json.dumps({'mime':mime,'audio':base64.b64encode(data or (b'RIFF'+b'0'*4+b'WAVE'+b'0'*200)).decode()}).encode()
 def test_invalid_audio_before_network(self):
  for b in [b'{}',b'{"mime":"audio/wav","audio":"https://evil.test"}',self.body('audio/wav',b'x'*150),self.body('text/html'),self.body(data=b'RIFF'+b'0'*4+b'WAVE'+b'0'*3145728)]:
   with self.assertRaises(worker.Failure):worker.transcription_request(b,'fixture')
 def test_fixed_model_and_exact_multipart(self):
  test=self
  class Response(io.BytesIO):
   def __enter__(self):return self
   def __exit__(self,*a):pass
  class Opener:
   def open(self,r,timeout):
    test.assertEqual(r.full_url,'https://api.openai.com/v1/audio/transcriptions');test.assertIn(b'gpt-transcribe',r.data);test.assertIn(b'name="languages[]"',r.data);test.assertIn(b'RIFF0000WAVE',r.data);return Response('{"text":"Проверка голоса"}'.encode())
  with patch.object(worker.urllib.request,'build_opener',return_value=Opener()):self.assertEqual(json.loads(worker.transcription_request(self.body(),'fixture'))['text'],'Проверка голоса')
 def test_route_rejects_unsigned_and_large(self):
  for body,expected in [(b'{}','401'),(b'x'*4300001,'413')]:
   status=[];env={'PATH_INFO':'/v1/ordnung/transcribe','REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body)}
   with patch.dict(worker.os.environ,{'OPENAI_API_KEY':'sk-fixture','WORKER_SHARED_SECRET':'x'*64,'OPENAI_MODEL':'fixture'}):worker.application(env,lambda s,h:status.append(s))
   self.assertTrue(status[0].startswith(expected))
if __name__=='__main__':unittest.main()
