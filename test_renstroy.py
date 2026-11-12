import io
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import worker


class RenstroyTests(unittest.TestCase):
    def setUp(self):
        worker._jobs.clear()
        worker._renstroy_jobs.clear()
        self.settings = patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test-placeholder', 'WORKER_SHARED_SECRET': 's' * 64, 'OPENAI_MODEL': 'gpt-6.1-terra'})
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.job = {'job_id': 'a' * 32, 'poll_token': 'b' * 64, 'ticket': 'c' * 64,
                    'payload_json': json.dumps({'instructions': 'Return JSON questions only.', 'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Example offer'}]}]})}

    def request(self, path, raw=None, method='POST', origin='https://renstroy.com'):
        body = json.dumps(raw or {}).encode()
        environ = {'REQUEST_METHOD': method, 'PATH_INFO': '/v1/renstroy/' + path, 'CONTENT_TYPE': 'application/json',
                   'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body), 'HTTP_ORIGIN': origin}
        response = []
        result = b''.join(worker.application(environ, lambda status, headers: response.extend([status, dict(headers)])))
        return int(response[0].split()[0]), json.loads(result), response[1]

    def test_preflight_does_not_disclose_credentials(self):
        status, body, headers = self.request('start', method='OPTIONS')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Access-Control-Allow-Origin'], 'https://renstroy.com')
        self.assertEqual(self.request('start', method='OPTIONS', origin='https://untrusted.test')[0], 403)

    def test_invalid_ticket_cannot_start_paid_call(self):
        with patch.object(worker, 'redeem_renstroy_ticket', side_effect=worker.Failure(401, 'invalid_renstroy_ticket')), patch.object(worker, 'request_openai') as paid:
            self.assertEqual(self.request('start', self.job)[0], 401)
            paid.assert_not_called()

    def test_start_duplicate_and_poll_pay_once(self):
        done = threading.Event()
        def model(*args):
            done.wait(2)
            return b'{"status":"completed","output":[]}'
        with patch.object(worker, 'redeem_renstroy_ticket') as redeem, patch.object(worker, 'request_openai', side_effect=model) as paid:
            self.assertEqual(self.request('start', self.job)[0], 202)
            self.assertEqual(self.request('start', self.job)[0], 202)
            redeem.assert_called_once()
            poll = {key: self.job[key] for key in ['job_id', 'poll_token']}
            self.assertEqual(self.request('result', {**poll, 'poll_token': 'd' * 64})[0], 401)
            self.assertEqual(self.request('result', poll)[0], 202)
            done.set()
            for _ in range(100):
                if worker._jobs[self.job['job_id']]['status'] != 'running':
                    break
                time.sleep(.005)
            self.assertEqual(self.request('result', poll)[0], 200)
            self.assertEqual(self.request('result', poll)[0], 200)
            self.assertEqual(paid.call_count, 1)

    def test_unknown_result_never_restarts(self):
        with patch.object(worker, 'request_openai') as paid:
            self.assertEqual(self.request('result', {'job_id': 'a' * 32, 'poll_token': 'b' * 64})[0], 404)
            paid.assert_not_called()

    def test_forbidden_tools_rejected_before_ticket_redemption(self):
        payload = json.loads(self.job['payload_json'])
        payload['tools'] = [{'type': 'web_search'}]
        with patch.object(worker, 'redeem_renstroy_ticket') as redeem:
            self.assertEqual(self.request('start', {**self.job, 'payload_json': json.dumps(payload)})[0], 400)
            redeem.assert_not_called()


if __name__ == '__main__':
    unittest.main()
