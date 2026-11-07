import urllib.request
import json

def test_login(email, pw):
    url = "http://127.0.0.1:8000/api/v1/auth/login"
    data = json.dumps({"identifier": email, "password": pw}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            print(f"SUCCESS {email} with '{pw}': user={res['data']['user']['email']}, role={res['data']['user']['role']}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"FAILED {email} with '{pw}': {e.code} - {body}")

# Test download
resp = urllib.request.urlopen("http://127.0.0.1:8000/download-zip")
print("Download endpoint status:", resp.status)
print("Content-Type:", resp.headers.get("content-type"))
print("Content-Length (bytes):", resp.headers.get("content-length"))
print("Content-Disposition:", resp.headers.get("content-disposition"))
