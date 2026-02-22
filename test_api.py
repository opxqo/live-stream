import urllib.request
import json
import traceback

def test_api():
    try:
        # get login token
        req = urllib.request.Request(
            'http://127.0.0.1:8088/api/login',
            data=json.dumps({"username": "admin", "password": "admin123"}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        res = urllib.request.urlopen(req)
        
        cookie = res.headers.get('Set-Cookie', '').split(';')[0]
        print("Got cookie:", cookie)
        
        # Test status
        req2 = urllib.request.Request(
            'http://127.0.0.1:8088/api/status',
            headers={'Cookie': cookie}
        )
        res2 = urllib.request.urlopen(req2)
        print("Status code (api/status):", res2.status)
        print("Response:", res2.read().decode('utf-8'))
        
    except Exception as e:
        print("Error!")
        print(e)
        if hasattr(e, 'read'):
            print(e.read().decode('utf-8'))

test_api()
