import os
import re
import yaml
import time
import json
import httpx
import base64
import asyncio
import uvicorn
import hashlib
import requests
import subprocess
import contextvars
import urllib.parse
import xml.sax.saxutils as saxutils
import xml.etree.ElementTree as ET

from database import db
from pydantic import BaseModel
from cachetools import LRUCache
from typing import Optional, Dict, Any, List
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from fastapi import FastAPI, Request, Response, Query, BackgroundTasks
from fastapi.responses import JSONResponse, Response, RedirectResponse, StreamingResponse

app = FastAPI(title="Apple Music Navidrome & Subsonic Bridge")

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)
GO_CWD_PATH = "./"

TEMP_DIR = "./temp_cache"
os.makedirs(TEMP_DIR, exist_ok=True)

http_client = httpx.AsyncClient(
    timeout=30.0, 
    limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
)

NAVIDROME_COVER_MAP = LRUCache(maxsize=10000)

http_client = httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_keepalive_connections=50, max_connections=100))
NAVIDROME_COVER_MAP = LRUCache(maxsize=10000)

_LOCAL_ALBUMS = None

def get_local_albums():
    global _LOCAL_ALBUMS
    if _LOCAL_ALBUMS is None:
        import json
        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(base_dir, "plus", "applemusic_所有女高音.json")
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f: 
                _LOCAL_ALBUMS = json.load(f)
            for item in _LOCAL_ALBUMS:
                url_str = item.get("url", "")
                al_id = ""
                if url_str:
                    match = re.search(r'/(\d+)(?:\?|$)', url_str)
                    if match: al_id = match.group(1)
                if not al_id: al_id = str(item.get("id", ""))
                if not al_id: continue
                
                pic_url = item.get("cover", item.get("pic_url", ""))
                if pic_url:
                    pic_url = pic_url.replace("http://", "https://")
                    NAVIDROME_COVER_MAP[al_id] = pic_url
                    NAVIDROME_COVER_MAP[f"virtual_mv_album_{al_id}"] = pic_url
                    NAVIDROME_COVER_MAP[f"artist_{al_id}"] = pic_url
        else:
            _LOCAL_ALBUMS = []
    return _LOCAL_ALBUMS

from apple_music_api import apple_api

def map_apple_song(apple_song: dict, force_artist_name: str = None, force_artist_id: str = None) -> dict:
    attr = apple_song.get("attributes", {})
    rels = apple_song.get("relationships", {})
    song_id = str(apple_song.get("id", ""))
    
    pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
    if pic_url: NAVIDROME_COVER_MAP[song_id] = pic_url
    
    artist_id = "0"
    if force_artist_id:
        artist_id = force_artist_id.replace("artist_", "")
    elif rels.get("artists", {}).get("data"):
        artist_id = str(rels["artists"]["data"][0].get("id", "0"))
    else:
        artist_url = attr.get("artistUrl", "")
        if artist_url:
            import re
            m = re.search(r'/artist/[^/]+/(\d+)', artist_url)
            if m: artist_id = m.group(1)
                
    album_id = "1"
    if rels.get("albums", {}).get("data"):
        album_id = str(rels["albums"]["data"][0].get("id", "1"))
    if album_id == "1":
        url_str = attr.get("url", "")
        if url_str:
            import re
            m = re.search(r'/album/[^/]+/(\d+)', url_str)
            if m: album_id = m.group(1)
                
    artist_name = force_artist_name or attr.get("artistName", "Unknown Artist")
    
    import urllib.parse
    if artist_id == "0" and artist_name and artist_name != "Unknown Artist":
        formatted_artist_id = f"artist_name_{urllib.parse.quote(artist_name)}"
    else:
        formatted_artist_id = f"artist_{artist_id}" if artist_id != "0" else "0"
        
    duration_sec = int(attr.get("durationInMillis", 0)) // 1000
    calc_size = int(duration_sec * 256 * 1000 / 8) if duration_sec > 0 else 5000000
    
    return {
        "id": song_id, "parent": album_id, "isDir": False,
        "title": attr.get("name", "Unknown Title"), "album": attr.get("albumName", "Unknown"),
        "albumId": album_id, "artist": artist_name, "artistId": formatted_artist_id,
        "track": attr.get("trackNumber", 1), "discNumber": attr.get("discNumber", 1), 
        "coverArt": song_id, "duration": duration_sec, "bitRate": 256, "suffix": "m4a", 
        "contentType": "audio/mp4", "type": "music", "size": calc_size, 
        "releaseDate": attr.get("releaseDate", "2024-01-01"), "isrc": attr.get("isrc", "")
    }

def get_current_user(request: Request) -> str:
    u = request.query_params.get("u")
    if u: return u
    
    auth_header = request.headers.get("authorization")
    if auth_header:
        if auth_header.startswith("Basic "):
            try:
                u, _ = base64.b64decode(auth_header[6:]).decode("utf-8").split(":", 1)
                return u
            except: pass
        elif auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if "." in token:
                try:
                    payload_b64 = token.split(".")[1]
                    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
                    return payload.get("username", "sky666")
                except: pass
                
    x_nd_token = request.headers.get("x-nd-authorization")
    if x_nd_token and "." in x_nd_token:
        try:
            payload_b64 = x_nd_token.split(".")[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
            return payload.get("username", "sky666")
        except: pass
        
    return "sky666"

ARTIST_ALBUM_CACHE = {} 

async def fetch_albums_incrementally(artist_id: str, cache_key: str, target_count: int):
    cache = ARTIST_ALBUM_CACHE.setdefault(cache_key, {"albums": [], "offset": 0, "status": "idle"})
    
    if cache["status"] == "done": return cache["albums"], True
    if len(cache["albums"]) >= target_count: return cache["albums"], False
        
    if cache["status"] == "fetching":
        for _ in range(15): 
            await asyncio.sleep(0.5)
            if cache["status"] != "fetching": break
        return cache["albums"], cache["status"] == "done"

    cache["status"] = "fetching"
    limit = 100
    pages_fetched_this_round = 0
    max_pages = 30 
    
    try:
        while len(cache["albums"]) < target_count and pages_fetched_this_round < max_pages:
            albums_data = await apple_api.get_artist_albums(artist_id, limit=limit, offset=cache["offset"])
            if not albums_data:
                cache["status"] = "done"
                break
                
            cache["albums"].extend(albums_data)
            cache["offset"] += limit
            pages_fetched_this_round += 1
            
            if len(albums_data) < limit: 
                cache["status"] = "done"
                break 
            await asyncio.sleep(0.2) 
            
    except Exception as e:
        print(f"❌ 增量拉取遇到波动: {e}")
    finally:
        if cache["status"] != "done": cache["status"] = "idle" 
            
    return cache["albums"], cache["status"] == "done"


async def proxy_image(url: str, fallback_url: str, request: Request):
    target_url = url if (url and url.startswith("http")) else fallback_url
    if not target_url: return Response(status_code=404)
    target_url = target_url.replace("http://", "https://")
    
    client_name = request.query_params.get("c", "").lower()
    if "amcfy" in client_name or "arrow" in client_name:
        try:
            res_obj = await http_client.get(target_url)
            return Response(
                content=res_obj.content, 
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=31536000"}
            )
        except: pass

    client_cache_headers = {
        "Cache-Control": "public, max-age=31536000",
        "Expires": "Thu, 31 Dec 2037 23:55:55 GMT"
    }
    return RedirectResponse(url=target_url, status_code=302, headers=client_cache_headers)       
        
def verify_user(username: str, password: str = None, token: str = None, salt: str = None) -> bool:
    if not username: return False
    user_file = "user.txt"
    if not os.path.exists(user_file):
        print("未找到 user.txt，拒绝登录")
        return False

    check_user_only = (not password and not token and not salt)
    with open(user_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or ":" not in line: continue
            u, p = line.split(":", 1)
            if u == username:
                if check_user_only: return True
                if password:
                    if p == password: return True
                    if password.startswith("enc:"):
                        try:
                            if p == bytes.fromhex(password[4:]).decode('utf-8'): return True
                        except: pass
                if token and salt:
                    expected_token = hashlib.md5((p + salt).encode('utf-8')).hexdigest()
                    if expected_token == token: return True
    return False

request_var = contextvars.ContextVar("request", default=None)

@app.middleware("http")
async def global_auth_middleware(request: Request, call_next):
    request_var.set(request)
    path = request.url.path
    if request.method == "OPTIONS": return await call_next(request)
    if not (path.startswith("/rest/") or path.startswith("/api/")): return await call_next(request)
    if path.startswith("/api/auth/") or path.startswith("/auth/login"): return await call_next(request)

    if path.startswith("/api/"):
        auth_header = request.headers.get("x-nd-authorization") or request.headers.get("authorization")
        jwt_cookie = request.cookies.get("jwt")
        if auth_header or jwt_cookie or request.method in ["GET", "OPTIONS"]:
            return await call_next(request)
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    u = request.query_params.get("u")
    p = request.query_params.get("p")
    t = request.query_params.get("t")
    s = request.query_params.get("s")
    
    if t == "": t = None
    if s == "": s = None
    
    if u and verify_user(username=u, password=p, token=t, salt=s):
        return await call_next(request)
        
    if t == "undefined" or s == "undefined" or (not p and not t and not s):
        if path.startswith("/rest/get") or path.startswith("/rest/stream") or \
           path.startswith("/rest/scrobble") or path.startswith("/rest/ping"):
            if u and verify_user(u):
                return await call_next(request)
                
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Basic "):
        try:
            import base64
            u, p = base64.b64decode(auth_header[6:]).decode("utf-8").split(":", 1)
            if verify_user(u, password=p): return await call_next(request)
        except: pass

    if request.query_params.get("f") == "json":
        return JSONResponse(status_code=200, content={"subsonic-response": {"status": "failed", "version": "1.16.1", "error": {"code": 40, "message": "Wrong username or password."}}})
    else:
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><subsonic-response xmlns="http://subsonic.org/restapi" status="failed" version="1.16.1"><error code="40" message="Wrong username or password."/></subsonic-response>', media_type="application/xml")

def dict_to_subsonic_xml(d: dict) -> str:
    def build_node(key, val):
        if isinstance(val, dict):
            attrs, children = [], []
            for k, v in val.items():
                if isinstance(v, (dict, list)): children.append(build_node(k, v))
                elif v is not None:
                    if isinstance(v, bool): v = str(v).lower()
                    attrs.append(f'{k}="{saxutils.escape(str(v))}"')
            attr_str = " " + " ".join(attrs) if attrs else ""
            child_str = "".join(children)
            if key == "subsonic-response" and "xmlns" not in attr_str:
                attr_str = ' xmlns="http://subsonic.org/restapi"' + attr_str
            if child_str: return f"<{key}{attr_str}>{child_str}</{key}>"
            else: return f"<{key}{attr_str}/>"
        elif isinstance(val, list):
            return "".join([build_node(key, item) for item in val])
        else:
            return f"<{key}>{saxutils.escape(str(val))}</{key}>"
    root_key = list(d.keys())[0]
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + build_node(root_key, d[root_key])

def build_subsonic_response(data: dict = None, error: dict = None):
    response_dict = {
        "subsonic-response": {
            "status": "ok" if not error else "failed",
            "version": "1.16.1",
            "type": "Apple Bridge",         
            "serverVersion": "0.1",   
            "openSubsonic": True         
        }
    }
    if error: response_dict["subsonic-response"]["error"] = error
    if data: response_dict["subsonic-response"].update(data)

    req = request_var.get()
    if req and req.query_params.get("f") != "json":
        xml_str = dict_to_subsonic_xml(response_dict)
        return Response(content=xml_str, media_type="application/xml")
        
    return JSONResponse(content=response_dict)

@app.api_route("/auth/login", methods=["GET", "POST"])
async def subsonic_login(request: Request):
    username = ""
    password = ""
    try:
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
    except:
        username = request.query_params.get("u", "")
        password = request.query_params.get("p", "")
        
    if not verify_user(username, password=password):
        return JSONResponse(status_code=401, content={"error": "Invalid credentials"})
        
    return {
        "id": username,        
        "username": username, 
        "name": username, 
        "email": f"{username}@apple.local", 
        "isAdmin": True, 
        "token": "fake_apple_token_666"
    }

@app.api_route("/rest/getUser", methods=["GET", "POST"])
@app.api_route("/rest/getUser.view", methods=["GET", "POST"])
async def sub_get_user(request: Request, username: str = Query(None)):
    u = username or get_current_user(request) 
    return build_subsonic_response({
        "user": {
            "username": u,
            "email": f"{u}@bridge.local",
            "scrobblingEnabled": True,
            "adminRole": True,
            "settingsRole": True,
            "downloadRole": True,
            "uploadRole": True,
            "playlistRole": True,
            "coverArtRole": True,
            "commentRole": True,
            "podcastRole": True,
            "streamRole": True,
            "jukeboxRole": True,
            "shareRole": True
        }
    })

@app.api_route("/rest/ping", methods=["GET", "POST"])
@app.api_route("/rest/ping.view", methods=["GET", "POST"])
async def ping(request: Request): return build_subsonic_response()

@app.api_route("/rest/getLicense", methods=["GET", "POST"])
@app.api_route("/rest/getLicense.view", methods=["GET", "POST"])
async def get_license(request: Request):
    return build_subsonic_response({"license": {"valid": True, "email": "user@example.com", "licenseExpires": "2099-12-31T23:59:59"}})

@app.api_route("/rest/getMusicFolders", methods=["GET", "POST"])
@app.api_route("/rest/getMusicFolders.view", methods=["GET", "POST"])
async def sub_get_music_folders(request: Request):
    return build_subsonic_response({"musicFolders": {"musicFolder": [{"id": 1, "name": "Apple Music"}]}})

@app.api_route("/rest/getArtists", methods=["GET", "POST"])
@app.api_route("/rest/getArtists.view", methods=["GET", "POST"])
@app.api_route("/rest/getIndexes", methods=["GET", "POST"])
@app.api_route("/rest/getIndexes.view", methods=["GET", "POST"])
async def get_artists_and_indexes(request: Request):
    if "getIndexes" in str(request.url):
        return build_subsonic_response({"indexes": {"ignoredArticles": "The", "lastModified": int(time.time() * 1000), "index": []}})
    else:
        return build_subsonic_response({"artists": {"ignoredArticles": "The", "index": []}})

@app.api_route("/rest/getAlbumList2", methods=["GET", "POST"])
@app.api_route("/rest/getAlbumList2.view", methods=["GET", "POST"])
async def get_album_list2(request: Request):
    try:
        size = int(request.query_params.get("size", 20))
        offset = int(request.query_params.get("offset", 0))
        
        classical_albums = await apple_api.get_top_100_albums()
        sliced_items = classical_albums[offset : offset + size]
        subsonic_albums = []
        
        import urllib.parse
        for al in sliced_items:
            attr = al.get("attributes", {})
            al_id = str(al["id"])
            pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
            if pic_url: NAVIDROME_COVER_MAP[al_id] = pic_url
            
            artist_id = "0"
            rels = al.get("relationships", {})
            if "artists" in rels and rels["artists"].get("data"):
                artist_id = str(rels["artists"]["data"][0].get("id", "0"))
                
            artist_name = attr.get("artistName", "Unknown")
            
            if artist_id == "0" and artist_name and artist_name != "Unknown":
                formatted_artist_id = f"artist_name_{urllib.parse.quote(artist_name)}"
            else:
                formatted_artist_id = f"artist_{artist_id}" if artist_id != "0" else "0"
                
            release_date = attr.get("releaseDate", "2024-01-01")
            year_val = 2024
            try: year_val = int(str(release_date)[:4])
            except: pass
            
            subsonic_albums.append({
                "id": al_id, "name": attr.get("name", "Unknown"), "artist": artist_name, 
                "artistId": formatted_artist_id, 
                "coverArt": al_id, "songCount": attr.get("trackCount", 10),
                "created": f"{release_date}T00:00:00.000Z", "year": year_val, "releaseDate": release_date 
            })
            
        return build_subsonic_response({"albumList2": {"album": subsonic_albums}})
    except Exception as e: 
        return build_subsonic_response(error={"code": 0, "message": str(e)})
    

@app.api_route("/rest/getScanStatus", methods=["GET", "POST"])
@app.api_route("/rest/getScanStatus.view", methods=["GET", "POST"])
async def sub_get_scan_status(request: Request):
    if request.query_params.get("type", "") == "navidrome":
        return Response(status_code=404)
    return build_subsonic_response({
        "scanStatus": {
            "scanning": False, "count": 1, "albumCount": 2, 
            "artistCount": 3, "folderCount": 4
        }
    })

def load_scrobble_config():
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f).get("scrobble", {})
    except:
        return {}

SCROBBLE_CONF = load_scrobble_config()

SCROBBLE_HISTORY = {}

async def submit_listenbrainz(artist: str, title: str, album: str, timestamp: int, is_now_playing: bool = False):
    conf = SCROBBLE_CONF.get("listenbrainz", {})
    if not conf.get("enable") or not conf.get("token"): return

    listen_type = "playing_now" if is_now_playing else "single"
    headers = {"Authorization": f"Token {conf['token']}"}
    payload = {"listen_type": listen_type, "payload": [{"listened_at": timestamp, "track_metadata": {"artist_name": artist, "track_name": title, "release_name": album}}]}
    try:
        async with httpx.AsyncClient() as client:
            await client.post("https://api.listenbrainz.org/1/submit-listens", json=payload, headers=headers)
            print(f"✅ [ListenBrainz] 同步成功: {artist} - {title} ({listen_type})")
    except Exception as e: pass

def sign_lastfm_request(params: dict, secret: str) -> str:
    sig_str = "".join([f"{k}{params[k]}" for k in sorted(params.keys())]) + secret
    return hashlib.md5(sig_str.encode('utf-8')).hexdigest()

async def submit_lastfm(artist: str, title: str, album: str, timestamp: int, is_now_playing: bool = False):
    conf = SCROBBLE_CONF.get("lastfm", {})
    if not conf.get("enable") or not conf.get("api_key"): return

    api_key, api_secret, sk = conf["api_key"], conf["api_secret"], conf.get("session_key")

    if not sk and conf.get("username") and conf.get("password"):
        print("🔄 [Last.fm] 正在换取 Session Key...")
        auth_params = {"method": "auth.getMobileSession", "username": conf["username"], "password": conf["password"], "api_key": api_key}
        auth_params["api_sig"] = sign_lastfm_request(auth_params, api_secret)
        auth_params["format"] = "json"
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post("https://ws.audioscrobbler.com/2.0/", data=auth_params)
                sk = res.json().get("session", {}).get("key")
                if sk:
                    SCROBBLE_CONF["lastfm"]["session_key"] = sk
                    print(f"🔑 [Last.fm] Session Key 获取成功，请填入 config.yaml: {sk}")
        except: return
    if not sk: return

    method = "track.updateNowPlaying" if is_now_playing else "track.scrobble"
    params = {"method": method, "api_key": api_key, "sk": sk, "artist": artist, "track": title, "album": album}
    if not is_now_playing: params["timestamp"] = str(timestamp)
    params["api_sig"] = sign_lastfm_request(params, api_secret)
    params["format"] = "json"

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post("https://ws.audioscrobbler.com/2.0/", data=params)
            if res.status_code == 200: print(f"✅ [Last.fm] 同步成功: {artist} - {title} ({method})")
    except: pass

@app.api_route("/rest/scrobble", methods=["GET", "POST"])
@app.api_route("/rest/scrobble.view", methods=["GET", "POST"])
async def scrobble(request: Request, background_tasks: BackgroundTasks):
    ids = request.query_params.getlist("id")
    times = request.query_params.getlist("time")
    client_submission = request.query_params.get("submission", "true").lower() == "true"
    
    if not ids or not SCROBBLE_CONF.get("enable"):
        return build_subsonic_response()

    async def process_scrobble():
        for i, song_id in enumerate(ids):
            try:
                if str(song_id).startswith("vido_") or str(song_id).startswith("mv_"):
                    continue
                    
                real_id = re.sub(r'\D', '', str(song_id))
                if not real_id: continue
                song_data = await apple_api.get_song_detail(real_id)
                attr = song_data.get("attributes", {})
                
                title = attr.get("name", "Unknown Title")
                artist = attr.get("artistName", "Unknown Artist")
                album = attr.get("albumName", "Unknown Album")
                
                duration_ms = attr.get("durationInMillis", 0)
                play_time_ms = 0
                if i < len(times) and times[i]:
                    try: play_time_ms = int(times[i])
                    except: pass
                
                now_timestamp = int(time.time())
                
                is_real_scrobble = client_submission
                if duration_ms > 0 and play_time_ms > 0:
                    if (play_time_ms / duration_ms) >= 0.10:
                        is_real_scrobble = True
                        
                if is_real_scrobble:
                    last_scrobble_time = SCROBBLE_HISTORY.get(real_id, 0)
                    min_interval = min((duration_ms // 1000) * 0.5, 60) if duration_ms > 0 else 60
                    
                    if now_timestamp - last_scrobble_time < min_interval:
                        is_real_scrobble = False 
                    else:
                        SCROBBLE_HISTORY[real_id] = now_timestamp
                
                await asyncio.gather(
                    submit_listenbrainz(artist, title, album, now_timestamp, not is_real_scrobble),
                    submit_lastfm(artist, title, album, now_timestamp, not is_real_scrobble)
                )
                
            except Exception as e:
                print(f"⚠️ [Scrobble] 处理歌曲 ID {song_id} 失败: {e}")

    background_tasks.add_task(process_scrobble)
    return build_subsonic_response()

@app.api_route("/rest/getGenres", methods=["GET", "POST"])
@app.api_route("/rest/getGenres.view", methods=["GET", "POST"])
async def get_genres(request: Request):
    return build_subsonic_response({
        "genres": {"genre": [{"value": "Apple Music", "songCount": 999, "albumCount": 999}]}
    })

@app.api_route("/rest/getCoverArt", methods=["GET", "POST"])
@app.api_route("/rest/getCoverArt.view", methods=["GET", "POST"])
async def get_cover_art(request: Request):
    id_str = request.query_params.get("id")
    fallback_image = "https://music.apple.com/assets/favicon/favicon-180.png"
    if not id_str or id_str == "0": return await proxy_image(fallback_image, fallback_image, request)

    if "-" in id_str and len(id_str) >= 32:
        tracks = db.get_playlist_tracks(id_str)
        found_cover = False
        if tracks:
            for track in reversed(tracks):
                cid = str(track.get("coverArt", "0"))
                if cid and cid != "0" and cid != id_str:
                    id_str = cid 
                    found_cover = True
                    break
        if not found_cover: return await proxy_image(fallback_image, fallback_image, request)

    if id_str in NAVIDROME_COVER_MAP:
        return await proxy_image(NAVIDROME_COVER_MAP[id_str], fallback_image, request)

    real_id = re.sub(r'\D', '', id_str)
    if real_id:
        try:
            if "artist_" in id_str:
                art = await apple_api.get_artist_detail(real_id)
                pic_url = art.get("attributes", {}).get("artwork", {}).get("url", "")
            elif id_str.startswith("al-") or "album" in request.url.path:
                al = await apple_api.get_album_detail(real_id)
                pic_url = al.get("attributes", {}).get("artwork", {}).get("url", "")
            else:
                song = await apple_api.get_song_detail(real_id)
                pic_url = song.get("attributes", {}).get("artwork", {}).get("url", "")

            pic_url = apple_api.format_artwork(pic_url)
            if pic_url:
                NAVIDROME_COVER_MAP[id_str] = pic_url
                return await proxy_image(pic_url, fallback_image, request)
        except: pass

    return await proxy_image(fallback_image, fallback_image, request)

@app.api_route("/rest/getSong", methods=["GET", "POST"])
@app.api_route("/rest/getSong.view", methods=["GET", "POST"])
async def get_song(request: Request):
    id_str = request.query_params.get("id")
    if not id_str: return build_subsonic_response(error={"code": 10, "message": "Required parameter is missing."})
    
    if isinstance(id_str, str) and (id_str.startswith("vido_") or id_str.startswith("nmv_") or id_str.startswith("mv_")):
        real_mv_id = id_str.replace("vido_", "").replace("nmv_", "").replace("mv_", "")
        display_artist, title_str = "Unknown Artist", f"官方影像_{real_mv_id}"
        return build_subsonic_response({"song": {
            "id": f"vido_{real_mv_id}",         
            "parent": "video_album_0",          
            "albumId": "video_album_0",          
            "isDir": False, "isVideo": True, "hasVideo": True,
            "title": title_str, "name": title_str,                 
            "album": "★ Videos ★", "artist": display_artist,
            "artistId": f"artist_{real_mv_id}",                  
            "albumArtist": display_artist, "albumArtistId": f"artist_{real_mv_id}",
            "coverArt": f"artist_{real_mv_id}", 
            "track": 1, "trackNumber": 1, "discNumber": 1,
            "duration": 300, "year": 2026, "created": "2026-01-01T00:00:00Z",
            "path": f"https://163.kimu.edu.kg/{display_artist}/{title_str}.mp4",
            "contentType": "video/mp4", "mimeType": "video/mp4", "suffix": "mp4",
            "type": "video", "mediaType": "Video", "bitRate": 2000, "size": 50000000
        }})

    try:
        real_id = re.sub(r'\D', '', id_str)
        if not real_id: return build_subsonic_response(error={"code": 10, "message": "Invalid ID."})
        song_data = await apple_api.get_song_detail(real_id)
        return build_subsonic_response({"song": map_apple_song(song_data)})
    except Exception as e: 
        return build_subsonic_response(error={"code": 0, "message": str(e)})

async def delete_after_ttl(filepath: str, ttl_seconds: int = 180):
    await asyncio.sleep(ttl_seconds)
    if os.path.exists(filepath):
        try: os.remove(filepath)
        except: pass

@app.api_route("/rest/stream", methods=["GET", "POST"])
@app.api_route("/rest/stream.view", methods=["GET", "POST"])
async def get_stream(request: Request, background_tasks: BackgroundTasks):
    import shutil 
    id_str = request.query_params.get("id")
    if not id_str: return Response(status_code=400)
    try:
        real_id = re.sub(r'\D', '', str(id_str))
        if not real_id: return Response(status_code=404)
        apple_url = f"https://music.apple.com/{apple_api.storefront}/song/{real_id}"
        
        abs_decrypted_path = os.path.abspath(os.path.join(TEMP_DIR, f"{real_id}.m4a"))
        
        if os.path.exists(abs_decrypted_path): 
            return FileResponse(abs_decrypted_path, media_type="audio/mp4", headers={"Accept-Ranges": "bytes"})
        
        async with DOWNLOAD_SEMAPHORE:
            task_out_dir = os.path.abspath(os.path.join(TEMP_DIR, f"task_{real_id}"))
            if os.path.exists(task_out_dir):
                shutil.rmtree(task_out_dir, ignore_errors=True)
                
            try:
                downloader_path = os.path.join(GO_CWD_PATH, "downloader")
                if not os.path.exists(downloader_path):
                    print(f"❌ [致命错误] 找不到可执行文件: {downloader_path}")
                    return Response(status_code=503)

                cmd = [
                    downloader_path, 
                    "--song",
                    "--aac", apple_url, 
                    "--output", task_out_dir  
                ]
                process = await asyncio.create_subprocess_exec(
                    *cmd, 
                    stdout=asyncio.subprocess.PIPE, 
                    stderr=asyncio.subprocess.PIPE,
                    cwd=GO_CWD_PATH  
                )
                stdout, stderr = await process.communicate()
                
                if process.returncode != 0:
                    error_msg = stderr.decode('utf-8').strip() or stdout.decode('utf-8').strip()
                    print(f"❌ [下载解密失败] {error_msg}")
                    return Response(status_code=503)
                
                found_audio = None
                for root, _, files in os.walk(task_out_dir):
                    for f in files:
                        if f.endswith(('.m4a', '.mp4', '.flac', '.alac')):
                            found_audio = os.path.join(root, f)
                            break
                    if found_audio: break
                
                if found_audio:
                    shutil.move(found_audio, abs_decrypted_path)
                    
            except Exception as e: 
                print(f"❌ [系统异常] 调用 Downloader 进程崩溃: {e}")
                return Response(status_code=500)
            finally:
                if os.path.exists(task_out_dir):
                    shutil.rmtree(task_out_dir, ignore_errors=True)

        if os.path.exists(abs_decrypted_path):
            background_tasks.add_task(delete_after_ttl, abs_decrypted_path, 180)
            return FileResponse(abs_decrypted_path, media_type="audio/mp4", headers={"Accept-Ranges": "bytes"})
        else:
            print(f"❌ [文件未生成] 找不到解密后的音频文件！")
            return Response(status_code=404)
            
    except Exception as e: 
        return Response(status_code=404)

@app.api_route("/rest/getLyrics", methods=["GET", "POST"])
@app.api_route("/rest/getLyrics.view", methods=["GET", "POST"])
@app.api_route("/rest/getLyricsBySongId", methods=["GET", "POST"])
@app.api_route("/rest/getLyricsBySongId.view", methods=["GET", "POST"])
async def get_lyrics(request: Request):
    id_str = request.query_params.get("id")
    artist = request.query_params.get("artist", "")
    title = request.query_params.get("title", "")

    if not id_str and title:
        try:
            search_res = await apple_api.search(f"{title} {artist}", limit=1)
            songs = search_res.get("songs", [])
            if songs: id_str = str(songs[0]["id"])
        except: pass
                
    if not id_str: 
        return build_subsonic_response(error={"code": 10, "message": "ID is required."})
        
    try:
        real_id = re.sub(r'\D', '', id_str)
        lrc_text = await apple_api.get_lyrics(real_id)
        if not lrc_text: return build_subsonic_response(error={"code": 70, "message": "Lyrics not found."})
        return build_subsonic_response({
            "lyricsList": {"lyrics": [{"id": id_str, "value": lrc_text}]},
            "lyrics": {"artist": artist, "title": title, "value": lrc_text}
        })
    except Exception as e: 
        return build_subsonic_response(error={"code": 0, "message": str(e)})


@app.api_route("/rest/getArtistInfo", methods=["GET", "POST"])
@app.api_route("/rest/getArtistInfo.view", methods=["GET", "POST"])
@app.api_route("/rest/getArtistInfo2", methods=["GET", "POST"])
@app.api_route("/rest/getArtistInfo2.view", methods=["GET", "POST"])
async def get_artist_info_all(request: Request, id: str = Query(...)):
    try:
        import urllib.parse
        import re
        
        if id.startswith("artist_name_"):
            query_name = urllib.parse.unquote(id.replace("artist_name_", ""))
            search_res = await apple_api.search(query_name, limit=1, types="artists")
            if search_res.get("artists"):
                real_id = str(search_res["artists"][0]["id"])
                id = f"artist_{real_id}"
            else:
                info_node = {"biography": "", "smallImageUrl": "", "mediumImageUrl": "", "largeImageUrl": "", "similarArtist": []}
                return build_subsonic_response({"artistInfo2": info_node} if "getArtistInfo2" in request.url.path else {"artistInfo": info_node})

        real_id = re.sub(r'\D', '', id)
        if not real_id or real_id == "0":
            info_node = {"biography": "", "smallImageUrl": "", "mediumImageUrl": "", "largeImageUrl": "", "similarArtist": []}
            return build_subsonic_response({"artistInfo2": info_node} if "getArtistInfo2" in request.url.path else {"artistInfo": info_node})
            
        artist_data = await apple_api.get_artist_detail(real_id)
        if not artist_data: raise Exception("Artist Not Found")
        
        attr = artist_data.get("attributes", {})
        artist_name = attr.get("name", "")
        
        desc = (
            attr.get("editorialNotes", {}).get("standard") or 
            attr.get("editorialNotes", {}).get("short") or 
            attr.get("description") or 
            artist_data.get("description") or 
            "暂无歌手简介"
        )
        desc = re.sub(r'<[^>]+>', '', desc).strip()
        
        pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
        if not pic_url:
            pic_url = await apple_api.get_artist_avatar(real_id)
            if pic_url: NAVIDROME_COVER_MAP[id] = pic_url
            
        similar_artists = await apple_api.get_similar_artists(real_id, artist_name)
        
        for art in similar_artists:
            if art.get("largeImageUrl"):
                NAVIDROME_COVER_MAP[art["id"]] = art["largeImageUrl"]
        
        info_node = {"biography": desc, "musicBrainzId": "", "lastFmUrl": "", "smallImageUrl": pic_url, "mediumImageUrl": pic_url, "largeImageUrl": pic_url, "similarArtist": similar_artists}
        return build_subsonic_response({"artistInfo2": info_node} if "getArtistInfo2" in request.url.path else {"artistInfo": info_node})
    except Exception as e: 
        print(f"❌ [Subsonic] 获取歌手详情异常: {e}")
        return Response(status_code=503, content="Service Unavailable.")

@app.api_route("/rest/search2", methods=["GET", "POST"])
@app.api_route("/rest/search2.view", methods=["GET", "POST"])
@app.api_route("/rest/search3", methods=["GET", "POST"])
@app.api_route("/rest/search3.view", methods=["GET", "POST"])
async def search3(request: Request):
    query = request.query_params.get("query", "")
    song_count = min(int(request.query_params.get("songCount", 20)), 251)
    
    search_result = {"song": [], "album": [], "artist": []}
    if not query or not query.strip():
        return build_subsonic_response({"searchResult3": search_result, "searchResult2": search_result})

    try:
        data = await apple_api.search(query, limit=song_count)

        if data["songs"]:
            search_result["song"] = [map_apple_song(s) for s in data["songs"]]

        if data["albums"]:
            for al in data["albums"]:
                attr = al.get("attributes", {})
                al_id = str(al["id"])
                pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
                NAVIDROME_COVER_MAP[al_id] = pic_url
                search_result["album"].append({
                    "id": al_id, "name": attr.get("name", "Unknown"),
                    "artist": attr.get("artistName", "Unknown"), "artistId": "0",
                    "coverArt": al_id, "songCount": attr.get("trackCount", 0),
                    "duration": 0, "created": "2024-01-01T00:00:00.000Z", 
                    "year": attr.get("releaseDate", "2024")[:4]
                })

        if data["artists"]:
            for ar in data["artists"]:
                attr = ar.get("attributes", {})
                ar_id = str(ar["id"])
                pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
                if pic_url:
                    NAVIDROME_COVER_MAP[ar_id] = pic_url
                    NAVIDROME_COVER_MAP[f"artist_{ar_id}"] = pic_url
                    
                search_result["artist"].append({
                    "id": ar_id, "name": attr.get("name", "Unknown"),
                    "coverArt": f"artist_{ar_id}", "artistImageUrl": pic_url,  
                    "albumCount": 1 
                })

        return build_subsonic_response({"searchResult3": search_result, "searchResult2": search_result})
        
    except Exception as e: 
        return build_subsonic_response(error={"code": 0, "message": str(e)})

@app.api_route("/rest/getAlbum", methods=["GET", "POST"])
@app.api_route("/rest/getAlbum.view", methods=["GET", "POST"])
@app.api_route("/rest/getMusicDirectory", methods=["GET", "POST"])
@app.api_route("/rest/getMusicDirectory.view", methods=["GET", "POST"])
async def get_album_and_dir(request: Request):
    id_str = request.query_params.get("id")
    if not id_str: return build_subsonic_response(error={"code": 10, "message": "ID is required."})
    if id_str.startswith("virtual_mv_album_"):
        real_artist_id = id_str.replace("virtual_mv_album_", "")
        try:
            albums = await apple_api.get_artist_albums(real_artist_id, limit=1)
            if albums:
                latest_album_id = str(albums[0]["id"])
                album_data = await apple_api.get_album_detail(latest_album_id)
                tracks_data = album_data.get("relationships", {}).get("tracks", {}).get("data", [])
                songs = [map_apple_song(s) for s in tracks_data]
                attr = album_data.get("attributes", {})
                album_node = {"id": id_str, "name": attr.get("name", "Latest Release"), "artist": attr.get("artistName", "Artist"), "artistId": f"artist_{real_artist_id}", "songCount": len(songs), "coverArt": latest_album_id, "song": songs}
                return build_subsonic_response({"album": album_node})
        except: pass
        return Response(status_code=404)
    
    try:
        real_id = re.sub(r'\D', '', id_str)
        if not real_id: return build_subsonic_response(error={"code": 10, "message": "Invalid ID."})
        album_data = await apple_api.get_album_detail(real_id)
        attr = album_data.get("attributes", {})
        tracks_data = album_data.get("relationships", {}).get("tracks", {}).get("data", [])
        songs = [map_apple_song(s) for s in tracks_data]
        artist_name = attr.get("artistName", "Unknown")
        if "getMusicDirectory" in request.url.path: return build_subsonic_response({"directory": {"id": id_str, "parent": "1", "name": attr.get("name", "Unknown"), "child": songs}})
        album_node = {"id": id_str, "name": attr.get("name", "Unknown"), "artist": artist_name, "artistId": "0", "songCount": len(songs), "coverArt": id_str, "comment": attr.get("editorialNotes", {}).get("standard", ""), "song": songs}
        return build_subsonic_response({"album": album_node})
    except Exception as e: return Response(status_code=503)

@app.api_route("/rest/getArtist", methods=["GET", "POST"])
@app.api_route("/rest/getArtist.view", methods=["GET", "POST"])
async def get_artist(request: Request):
    id_str = request.query_params.get("id")
    if not id_str: return build_subsonic_response(error={"code": 10, "message": "ID is required."})
    if id_str.startswith("similar-"): return build_subsonic_response({"artist": {"id": id_str, "name": "Similar Artists", "albumCount": 0, "album": []}})
    
    import urllib.parse
    import re
    
    if id_str.startswith("artist_name_"):
        query_name = urllib.parse.unquote(id_str.replace("artist_name_", ""))
        search_res = await apple_api.search(query_name, limit=1, types="artists")
        if search_res.get("artists"):
            real_id = str(search_res["artists"][0]["id"])
            id_str = f"artist_{real_id}"
        else:
            return build_subsonic_response({"artist": {"id": id_str, "name": query_name, "albumCount": 0, "album": []}})

    real_id = re.sub(r'\D', '', id_str)
    artist_cache_key = f"artist_{real_id}"
    
    try:
        subsonic_albums = []
        artist_data = await apple_api.get_artist_detail(real_id)
        artist_name = artist_data.get("attributes", {}).get("name", "Artist")

        virtual_mv_album_id = f"video_album_{real_id}"
        subsonic_albums.append({
            "id": virtual_mv_album_id, "name": "★ Videos ★", "artist": artist_name,                
            "artistId": artist_cache_key, "coverArt": artist_cache_key, 
            "songCount": 0, "year": 2099, "created": "2099-01-01T00:00:00.000Z" 
        })

        all_valid_albums_raw = await apple_api.get_artist_albums(real_id, limit=100)
        
        for al in all_valid_albums_raw:
            attr = al.get("attributes", {})
            if attr.get("isSingle", False) == True: continue
            
            al_artist = attr.get("artistName", "")
            if artist_name.lower() not in al_artist.lower(): continue

            al_id = str(al["id"])
            pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
            NAVIDROME_COVER_MAP[al_id] = pic_url
            subsonic_albums.append({
                "id": al_id, "name": attr.get("name", "Unknown"),
                "artist": artist_name, "artistId": artist_cache_key,
                "coverArt": al_id, "songCount": attr.get("trackCount", 0), 
                "created": "2024-01-01T00:00:00.000Z"
            })

        return build_subsonic_response({
            "artist": {
                "id": artist_cache_key, "name": artist_name, 
                "albumCount": len(subsonic_albums), "coverArt": artist_cache_key, 
                "album": subsonic_albums
            }
        })
    except Exception as e: 
        return Response(status_code=503, content="Service Unavailable.")

def format_subsonic_playlist(pl: dict, current_user: str):
    tracks = db.get_playlist_tracks(pl["id"])
    cover_id = str(pl["id"]) 
    if tracks:
        for track in reversed(tracks):
            cid = str(track.get("coverArt", "0"))
            if cid and cid != "0":
                cover_id = cid
                break
    real_owner = pl.get("owner") or current_user 
    return {
        "id": str(pl["id"]), "name": str(pl.get("name", "")), "owner": real_owner,
        "public": False, "songCount": int(pl.get("song_count", 0)), "duration": int(pl.get("duration", 0)),
        "created": time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(pl.get("created", time.time()))),
        "changed": time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(pl.get("updated", time.time()))),
        "coverArt": cover_id, "allowedUser": True, "isOwner": True      
    }

@app.api_route("/rest/getPlaylist", methods=["GET", "POST"])
@app.api_route("/rest/getPlaylist.view", methods=["GET", "POST"])
async def sub_get_playlist(request: Request, id: str = Query(...)):
    current_user = get_current_user(request) 
    pl = await run_in_threadpool(db.get_playlist, id)
    if not pl: return build_subsonic_response(error={"code": 70, "message": "Playlist not found"})

    tracks = await run_in_threadpool(db.get_playlist_tracks, id)
    cover_id = str(id)
    if tracks:
        for track in reversed(tracks):
            cid = str(track.get("coverArt", "0"))
            if cid and cid != "0":
                cover_id = cid
                break

    formatted_tracks = []
    for t in tracks:
        if "id" in t: t["id"] = str(t["id"])
        formatted_tracks.append(t)

    real_owner = pl.get("owner") or current_user
    pl_data = {
        "id": str(pl["id"]), "name": str(pl.get("name", "")), "owner": real_owner, "public": False,
        "songCount": int(pl.get("song_count", 0)), "duration": int(pl.get("duration", 0)),
        "created": time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(pl.get("created", time.time()))),
        "coverArt": cover_id, "allowedUser": True, "isOwner": True, "entry": formatted_tracks
    }
    return build_subsonic_response({"playlist": pl_data})

@app.api_route("/rest/getPlaylists", methods=["GET", "POST"])
@app.api_route("/rest/getPlaylists.view", methods=["GET", "POST"])
async def sub_get_playlists(request: Request):
    current_user = get_current_user(request) 
    pls = db.get_playlists()
    formatted_pls = [format_subsonic_playlist(p, current_user) for p in pls]
    return build_subsonic_response({"playlists": {"playlist": formatted_pls}})

@app.api_route("/rest/updatePlaylist", methods=["GET", "POST"])
@app.api_route("/rest/updatePlaylist.view", methods=["GET", "POST"])
async def sub_update_playlist(request: Request, playlistId: str = Query(...)):
    name = request.query_params.get("name")
    song_ids_to_add = request.query_params.getlist("songIdToAdd")
    song_indexes_to_remove = request.query_params.getlist("songIndexToRemove")

    if name: db.update_playlist_name(playlistId, name)

    if song_indexes_to_remove:
        indexes = sorted([int(i) for i in song_indexes_to_remove], reverse=True)
        for idx in indexes: db.remove_track_from_playlist(playlistId, idx)

    if song_ids_to_add:
        real_sids = [re.sub(r'\D', '', str(sid)) for sid in song_ids_to_add if re.sub(r'\D', '', str(sid))]
        if real_sids:
            try:
                tracks_to_add = []
                for sid in real_sids:
                    s_data = await apple_api.get_song_detail(sid)
                    tracks_to_add.append(map_apple_song(s_data))
                if tracks_to_add:
                    db.add_tracks_to_playlist(playlistId, tracks_to_add)
            except: pass
    return build_subsonic_response()

@app.api_route("/rest/createPlaylist", methods=["GET", "POST"])
@app.api_route("/rest/createPlaylist.view", methods=["GET", "POST"])
async def sub_create_playlist(request: Request):
    pl_id = request.query_params.get("playlistId")
    name = request.query_params.get("name")
    song_ids = request.query_params.getlist("songId")
    current_user = get_current_user(request) 

    if not pl_id and name: pl_id = db.create_playlist(name, owner=current_user)
    elif pl_id and name: db.update_playlist_name(pl_id, name)
        
    if not pl_id: return build_subsonic_response(error={"code": 10, "message": "Playlist ID or name required."})

    if song_ids:
        real_sids = [re.sub(r'\D', '', str(sid)) for sid in song_ids if re.sub(r'\D', '', str(sid))]
        if real_sids:
            try:
                tracks_to_add = []
                for sid in real_sids:
                    s_data = await apple_api.get_song_detail(sid)
                    tracks_to_add.append(map_apple_song(s_data))
                if tracks_to_add:
                    db.add_tracks_to_playlist(pl_id, tracks_to_add)
            except: pass
            
    pl = db.get_playlist(pl_id)
    return build_subsonic_response({"playlist": format_subsonic_playlist(pl, current_user)})

@app.api_route("/rest/deletePlaylist", methods=["GET", "POST"])
@app.api_route("/rest/deletePlaylist.view", methods=["GET", "POST"])
async def sub_delete_playlist(request: Request, id: str = Query(...)):
    db.delete_playlist(id)
    return build_subsonic_response()    
    
@app.api_route("/rest/{path:path}", methods=["GET", "POST"])
async def catch_all_subsonic_routes(request: Request, path: str):
    return build_subsonic_response()

@app.get("/video.html")
async def serve_video_html():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base_dir, "static", "video.html"))

@app.get("/player-feishin.js")
async def serve_player_feishin_js():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base_dir, "static", "player-feishin.js"))

@app.api_route("/rest/custom_lrc.view", methods=["GET", "POST"])
@app.api_route("/netease_proxy/rest/custom_lrc.view", methods=["GET", "POST"])
async def custom_lrc_api(request: Request):
    from urllib.parse import unquote
    from difflib import SequenceMatcher
    from fastapi.responses import Response

    params = dict(request.query_params)
    title = params.get("title", "").strip()
    artist = params.get("artist", "").strip()

    if not title or not artist:
        raw_qs = request.scope.get("query_string", b"").decode()
        if not title:
            t_match = re.search(r"title=([^&]+)", raw_qs)
            if t_match: title = unquote(t_match.group(1))
        if not artist:
            a_match = re.search(r"artist=([^&]+)", raw_qs)
            if a_match: artist = unquote(a_match.group(1))

    if not title:
        return Response(content="[00:00.00] 等待获取歌曲信息...", media_type="text/plain; charset=utf-8")

    def clean(text):
        if not text: return ""
        text = re.sub(r'\(.*?\)|\[.*?\]', '', text)
        return " ".join(text.split()).lower()

    target_title, target_artist = clean(title), clean(artist)
    def similarity(a, b): return SequenceMatcher(None, a, b).ratio()

    try:
        search_res = await apple_api.search(f"{title} {artist}", limit=20)
        candidates = search_res.get("songs", [])
        if not candidates: 
            return Response(content=f"[00:00.00] 未找到歌曲: {title}", media_type="text/plain; charset=utf-8")

        best_song, best_score = None, 0
        filtered = []
        target_artist_words = target_artist.split()

        for s in candidates:
            artist_names = s.get("attributes", {}).get("artistName", "").lower()
            if target_artist_words:
                match_count = sum(1 for w in target_artist_words if w in artist_names)
                if match_count >= 1: filtered.append(s)

        if not filtered: filtered = candidates

        for s in filtered:
            s_name = clean(s.get("attributes", {}).get("name", ""))
            score = similarity(target_title, s_name)
            if target_title == s_name: score += 0.5
            elif target_title in s_name or s_name in target_title: score += 0.2
            if score > best_score: best_score, best_song = score, s

        if not best_song or best_score < 0.55:
            return Response(content=f"[00:00.00] 匹配失败: {title}", media_type="text/plain; charset=utf-8")

        lrc_text = await apple_api.get_lyrics(str(best_song["id"]))
        if not lrc_text: return Response(content="[00:00.00] 暂无歌词", media_type="text/plain; charset=utf-8")
        return Response(content=lrc_text, media_type="text/plain; charset=utf-8")
    except Exception as e:
        return Response(content="[00:00.00] 歌词服务异常", media_type="text/plain; charset=utf-8")

try:
    from navidrome import router as navidrome_router
    app.include_router(navidrome_router)
except ImportError:
    pass

if __name__ == "__main__":
    print("🚀 Apple Music Navidrome & Subsonic Bridge Starting...")
    print("👉 Serving on http://0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
