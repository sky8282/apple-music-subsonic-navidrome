import hashlib
import requests

api_key = "你的key"
api_secret = "你的key"

res = requests.get("https://ws.audioscrobbler.com/2.0/", params={
    "method": "auth.getToken",
    "api_key": api_key,
    "format": "json"
}).json()

token = res["token"]
print("✅ Token:", token)

auth_url = f"https://www.last.fm/api/auth/?api_key={api_key}&token={token}"

print("\n🌐 请复制到浏览器打开下面这个 URL 并点击 Allow\n同时不要关闭授权页面\n")
print(auth_url)

input("\n👉 授权完成后，回到这里按回车继续...")

sig_str = f"api_key{api_key}methodauth.getSessiontoken{token}{api_secret}"
api_sig = hashlib.md5(sig_str.encode()).hexdigest()

res = requests.get("https://ws.audioscrobbler.com/2.0/", params={
    "method": "auth.getSession",
    "api_key": api_key,
    "token": token,
    "api_sig": api_sig,
    "format": "json"
}).json()

print("\n🎉 结果：")
print(res)

if "session" in res:
    print("\n🔥 session_key =", res["session"]["key"])
else:
    print("\n❌ 获取失败：", res)