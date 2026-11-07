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

print("Testing against live server http://127.0.0.1:8000:")
test_login("25ag1a6767@aceec.ac.in", "ace@25ag1a67676")
test_login("25ag1a6767@aceec.ac.in", "ace@25ag1a6767")
test_login("25ag1a6767@gmail.com", "ace@25ag1a67676staff")
test_login("25ag1a6767@gmail.com", "ace@25ag1a6767staff")

# Check /admin HTML
admin_html = urllib.request.urlopen("http://127.0.0.1:8000/admin").read().decode("utf-8")
print("\nAdmin HTML inspection:")
print("  Contains 'admin-totp'?", "admin-totp" in admin_html)
print("  Contains '25ag1a6767@aceec.ac.in'?", "25ag1a6767@aceec.ac.in" in admin_html)

# Check /staff HTML
staff_html = urllib.request.urlopen("http://127.0.0.1:8000/staff").read().decode("utf-8")
print("\nStaff HTML inspection:")
print("  Contains 'staff-mfa-wrap'?", "staff-mfa-wrap" in staff_html)
print("  Contains '25ag1a6767@gmail.com'?", "25ag1a6767@gmail.com" in staff_html)

# Check /api/v1/counters
counters_res = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/counters").read().decode("utf-8"))
print(f"\nLive Active Counters ({len(counters_res['data']['items'])}):")
for c in counters_res['data']['items']:
    print(f"  ID {c['id']}: {c['name']} ({c['location']})")
