import io,json,unittest
from unittest.mock import patch
import worker

class VoiceTests(unittest.TestCase):
    def test_validation_blocks_urls_and_invalid_sdp(self):
        for body in [{}, {'sdp':'https://x','instructions':'Tutor','user':'a'*64}, {'call_id':'../secrets'}]:
            with self.assertRaises(worker.Failure):worker.realtime_request(json.dumps(body).encode(),'fixture',end='call_id' in body)
    def test_server_owned_model_and_bounded_session(self):
        response=io.BytesIO(b'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n')
        response.headers={'Location':'/v1/realtime/calls/rtc_abcdefgh1234'}
        class Context:
            def __enter__(self):return response
            def __exit__(self,*args):pass
        class Opener:
            def open(self,request,timeout):
                self_request=request.data.decode();self_test.assertIn('gpt-realtime-2.1-mini',self_request);self_test.assertIn('interrupt_response',self_request);self_test.assertIn('8000',self_request);self_test.assertNotIn('api_key',self_request);return Context()
        self_test=self
        with patch.object(worker.urllib.request,'build_opener',return_value=Opener()):
            result=json.loads(worker.realtime_request(json.dumps({'sdp':'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n','instructions':'Tutor','user':'a'*64}).encode(),'fixture'))
        self.assertEqual(result['call_id'],'rtc_abcdefgh1234');self.assertNotIn('key',result)
    def test_voice_route_is_authenticated(self):
        result=[]
        env={'PATH_INFO':'/v1/academy/realtime','REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':'2','wsgi.input':io.BytesIO(b'{}')}
        with patch.dict(worker.os.environ,{'OPENAI_API_KEY':'sk-fixture','WORKER_SHARED_SECRET':'x'*64,'OPENAI_MODEL':'fixture'}):
            worker.application(env,lambda status,headers:result.append(status))
        self.assertTrue(result[0].startswith('401'))

if __name__=='__main__':unittest.main()
