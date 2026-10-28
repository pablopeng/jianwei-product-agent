import base64
from http.server import ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from agent.web import Handler, serve

class CloudDeploymentTests(unittest.TestCase):
    def test_cloud_auth_host_origin_and_secure_cookie(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        password='test-only-password-1234'
        def call(auth=None,origin=None,host='demo.example'):
            headers={'Host':host}
            if auth:headers['Authorization']='Basic '+base64.b64encode(auth.encode()).decode()
            if origin:headers['Origin']=origin
            req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/status',headers=headers)
            try:
                with urllib.request.urlopen(req) as r:return r.status,dict(r.headers),json.load(r)
            except urllib.error.HTTPError as e:return e.code,dict(e.headers),e.read()
        try:
            with patch.dict(os.environ,{'AGENT_DEPLOYMENT':'cloud','AGENT_ALLOWED_HOSTS':'demo.example','DEMO_ACCESS_PASSWORD':password}):
                self.assertEqual(call()[0],401)
                self.assertEqual(call('demo:wrong')[0],401)
                status,headers,data=call('demo:'+password,'https://demo.example')
                self.assertEqual(status,200);self.assertIn('Secure',headers['Set-Cookie'])
                self.assertNotIn('DEEPSEEK_API_KEY',data['model'])
                self.assertEqual(call('demo:'+password,'https://evil.example')[0],403)
                self.assertEqual(call('demo:'+password,host='evil.example')[0],403)
        finally:server.shutdown();server.server_close()
    def test_cloud_fails_closed_without_access_configuration(self):
        with patch.dict(os.environ,{'AGENT_DEPLOYMENT':'cloud','AGENT_ALLOWED_HOSTS':'','DEMO_ACCESS_PASSWORD':''}):
            with self.assertRaises(ValueError):serve(0,'0.0.0.0')
