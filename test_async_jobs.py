import hashlib,hmac,io,json,os,threading,time,unittest,uuid
from unittest.mock import patch
import worker

SECRET='unit-test-shared-secret-'*4
REQUEST={'instructions':'Return JSON','input':[{'role':'user','content':[{'type':'input_text','text':'test fixture'}]}]}
RESULT=b'{"status":"completed","output":[]}'

def call(path,data):
    body=json.dumps(data).encode();stamp=str(int(time.time()));nonce=uuid.uuid4().hex
    sig=hmac.new(SECRET.encode(),stamp.encode()+b'\n'+nonce.encode()+b'\n'+body,hashlib.sha256).hexdigest()
    env={'PATH_INFO':path,'REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body),'HTTP_X_GOSPLAN_TIMESTAMP':stamp,'HTTP_X_GOSPLAN_NONCE':nonce,'HTTP_X_GOSPLAN_SIGNATURE':sig}
    status=[];response=b''.join(worker.application(env,lambda s,h:status.append(int(s.split()[0]))))
    return status[0],response

class AsyncJobsTest(unittest.TestCase):
    def setUp(self):
        worker._jobs.clear();worker._nonces.clear();worker._calls.clear();worker._poll_calls.clear();worker._slots=threading.BoundedSemaphore(2)
        self.env=patch.dict(os.environ,{'OPENAI_API_KEY':'sk-unit-test-no-real-api','WORKER_SHARED_SECRET':SECRET,'OPENAI_MODEL':'fixture-model'});self.env.start()
    def tearDown(self):self.env.stop()
    def test_submit_returns_before_completion_and_duplicate_pays_once(self):
        finish=threading.Event();started=threading.Event();job=uuid.uuid4().hex
        def upstream(*args):started.set();finish.wait(3);return RESULT
        with patch.object(worker,'request_openai',side_effect=upstream) as upstream_mock:
            before=time.monotonic();self.assertEqual(call('/v1/jobs',{'job_id':job,'request':REQUEST})[0],202)
            self.assertLess(time.monotonic()-before,0.5);self.assertTrue(started.wait(1))
            self.assertEqual(call('/v1/jobs',{'job_id':job,'request':REQUEST})[0],202)
            self.assertEqual(call('/v1/jobs/result',{'job_id':job})[0],202)
            finish.set()
            for _ in range(100):
                if worker._jobs[job]['status']=='succeeded':break
                threading.Event().wait(0.005)
            self.assertEqual(call('/v1/jobs/result',{'job_id':job}),(200,RESULT))
            self.assertEqual(upstream_mock.call_count,1)
    def test_conflicting_duplicate_is_rejected(self):
        job=uuid.uuid4().hex
        with patch.object(worker,'request_openai',return_value=RESULT):
            self.assertEqual(call('/v1/jobs',{'job_id':job,'request':REQUEST})[0],202)
            changed={**REQUEST,'instructions':'Different JSON'}
            self.assertEqual(call('/v1/jobs',{'job_id':job,'request':changed})[0],409)
            for _ in range(100):
                if worker._jobs[job]['status']!='running':break
                threading.Event().wait(0.005)
    def test_unknown_job_does_not_start_a_paid_call(self):
        with patch.object(worker,'request_openai') as upstream:
            self.assertEqual(call('/v1/jobs/result',{'job_id':uuid.uuid4().hex})[0],404)
            upstream.assert_not_called()
    def test_provider_failure_is_available_to_poller(self):
        job=uuid.uuid4().hex
        with patch.object(worker,'request_openai',side_effect=worker.Failure(502,'upstream_timeout')):
            self.assertEqual(call('/v1/jobs',{'job_id':job,'request':REQUEST})[0],202)
            for _ in range(100):
                if worker._jobs[job]['status']=='failed':break
                threading.Event().wait(0.005)
            status,body=call('/v1/jobs/result',{'job_id':job})
            self.assertEqual(status,502);self.assertEqual(json.loads(body)['error']['code'],'upstream_timeout')
    def test_invalid_payload_does_not_occupy_capacity(self):
        with patch.object(worker,'request_openai') as upstream:
            self.assertEqual(call('/v1/jobs',{'job_id':uuid.uuid4().hex,'request':{}})[0],400)
            self.assertEqual(worker._jobs,{})
            upstream.assert_not_called()

if __name__=='__main__':unittest.main()
