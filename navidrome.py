import os
import re
import json
import time
import hmac
import httpx
import base64
import hashlib
import asyncio
import requests
import datetime

from database import db
from fastapi import Body, APIRouter, Request, Query, Response, BackgroundTasks
from cachetools import LRUCache
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse, FileResponse
from subsonic import (
    verify_user, 
    NAVIDROME_COVER_MAP, 
    get_current_user,
    apple_api,
    DOWNLOAD_SEMAPHORE,
    TEMP_DIR,
    GO_CWD_PATH,
    delete_after_ttl,
    get_local_albums
)

router = APIRouter(prefix="/api")
GLOBAL_LOCAL_ALBUMS_NAV = None

http_client = httpx.AsyncClient(
    timeout=30.0, 
    limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
)

def create_list_response(data: list, total_count: int, start: int = 0):
    count_in_page = len(data)
    
    if total_count == 0:
        range_str = "items */0"
    elif count_in_page == 0:
        range_str = f"items */{total_count}"
    else:
        current_end_index = start + count_in_page - 1
        range_str = f"items {start}-{current_end_index}/{total_count}"
        
    headers = {
        "X-Total-Count": str(total_count),
        "x-total-count": str(total_count),
        "Content-Range": range_str,
        "Access-Control-Expose-Headers": "X-Total-Count, Content-Range, x-total-count"
    }
    return Response(content=json.dumps(data), media_type="application/json", headers=headers)

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

def generate_nav_jwt(username: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "id": "1",
        "username": username,
        "isAdmin": True,
        "iat": int(time.time()),
        "exp": int(time.time()) + 86400 * 365 
    }
    
    header_b64 = base64.urlsafe_b64encode(json.dumps(header, separators=(',', ':')).encode()).decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip("=")
    
    msg = f"{header_b64}.{payload_b64}"
    secret = b"apple_music_dummy_secret_666"
    sig = hmac.new(secret, msg.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    
    return f"{msg}.{sig_b64}"

@router.api_route("/auth/login", methods=["GET", "POST"])
async def nav_login(request: Request):
    username = ""
    password = ""
    try:
        if request.method == "POST":
            data = await request.json()
            username = data.get("username", "")
            password = data.get("password", "")
        else:
            username = request.query_params.get("u", "")
            password = request.query_params.get("p", "")
    except: 
        pass
        
    if not verify_user(username, password=password):
        return Response(content=json.dumps({"error": "Unauthorized"}), status_code=401, media_type="application/json")

    jwt_token = generate_nav_jwt(username)

    resp = JSONResponse(content={
        "id": username,
        "username": username, 
        "name": username,
        "email": f"{username}@apple.local", 
        "isAdmin": True, 
        "token": jwt_token
    })
    resp.set_cookie(key="jwt", value=jwt_token, max_age=86400*365, httponly=False, samesite="lax")
    return resp

@router.get("/library")
async def nav_get_libraries(request: Request, _start: int = Query(0)):
    lib_obj = {
        "id": "1", 
        "name": "Apple Bridge", 
        "isDefault": True,
        "folderCount": 1,
        "songCount": 50000000,
        "albumCount": 2000000,
        "artistCount": 500000,
        "lastScan": "2024-01-01T00:00:00Z",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z"
    }
    return create_list_response([lib_obj], 1, _start)

@router.get("/genre")
async def nav_get_genres(request: Request, _start: int = Query(0)):
    dummy_genre = {"id": "Apple Music", "name": "Apple Music", "songCount": 999, "albumCount": 999}
    return create_list_response([dummy_genre], 1, _start)

@router.get("/radio")
async def nav_get_radios(request: Request, _start: int = Query(0)):
    return create_list_response([], 0, _start)

@router.get("/keepalive/keepalive")
async def nav_keepalive():
    return {"status": "ok"}

@router.get("/tag")
async def nav_get_tags(request: Request, _start: int = Query(0)):
    return create_list_response([], 0, _start)

def map_nav_album(al: dict, force_artist_name: str = None, force_artist_id: str = None) -> dict:
    attr = al.get("attributes", {})
    rels = al.get("relationships", {})
    real_al_id = str(al.get("id", "0"))
    
    artist_id = "0"
    if force_artist_id:
        artist_id = force_artist_id.replace("artist_", "")
    elif rels.get("artists", {}).get("data"):
        artist_id = str(rels["artists"]["data"][0].get("id", "0"))
    else:
        artist_url = attr.get("artistUrl", "")
        if artist_url:
            import re
            m = re.search(r'/artist/(?:[^/]+/)?(\d+)', artist_url)
            if m: artist_id = m.group(1)
            
    artist_name = force_artist_name or attr.get("artistName", "Unknown Artist")
    
    if artist_id == "0" and artist_name and artist_name != "Unknown Artist":
        formatted_artist_id = f"artist_name_{artist_name}"
    else:
        formatted_artist_id = f"artist_{artist_id}" if artist_id != "0" else "0"
    
    final_id = f"{real_al_id}_ctx_{artist_id}" if force_artist_id and artist_id != "0" else real_al_id
    
    pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
    if pic_url: 
        NAVIDROME_COVER_MAP[real_al_id] = pic_url
        if force_artist_id: 
            NAVIDROME_COVER_MAP[final_id] = pic_url

    release_date = attr.get("releaseDate", "2024-01-01")
    year_val = 2024
    try: year_val = int(release_date[:4])
    except: pass
    created_val = f"{release_date}T00:00:00Z"

    return {
        "id": final_id, "name": attr.get("name", "Unknown Album"), "title": attr.get("name", "Unknown Album"),
        "artist": artist_name, "artistId": formatted_artist_id,
        "albumArtist": artist_name, "albumArtistId": formatted_artist_id,
        "coverArt": real_al_id, "songCount": attr.get("trackCount", 1), 
        "description": attr.get("editorialNotes", {}).get("standard", ""), 
        "comment": attr.get("editorialNotes", {}).get("standard", ""),      
        "isDir": True, "year": year_val, "minYear": year_val, "maxYear": year_val,
        "created": created_val, "createdAt": created_val, "updatedAt": created_val,    
        "playDate": created_val, "play_date": created_val, "releaseDate": release_date,   
        "genre": "Apple Music", "plays": 0, "playCount": 0, "duration": 0        
    }

def map_nav_song(s: dict, default_album_id: str = "1", force_artist_name: str = None, force_artist_id: str = None) -> dict:
    attr = s.get("attributes", {})
    rels = s.get("relationships", {})
    s_id = str(s.get("id", ""))
    album_name = attr.get("albumName", "Unknown Album")
    
    pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
    if pic_url: NAVIDROME_COVER_MAP[s_id] = pic_url
    
    album_id = default_album_id
    if "_ctx_" not in default_album_id: 
        if rels.get("albums", {}).get("data"):
            album_id = str(rels["albums"]["data"][0].get("id", default_album_id))
        if album_id == "1":
            url_str = attr.get("url", "")
            if url_str:
                import re
                m = re.search(r'/album/(?:[^/]+/)?(\d+)', url_str)
                if m: album_id = m.group(1)
            
    artist_id = "0"
    if force_artist_id:
        artist_id = force_artist_id.replace("artist_", "")
    elif rels.get("artists", {}).get("data"):
        artist_id = str(rels["artists"]["data"][0].get("id", "0"))
    else:
        artist_url = attr.get("artistUrl", "")
        if artist_url:
            import re
            m = re.search(r'/artist/(?:[^/]+/)?(\d+)', artist_url)
            if m: artist_id = m.group(1)
            
    artist_name = force_artist_name or attr.get("artistName", "Unknown Artist")
    
    if artist_id == "0" and artist_name != "Unknown Artist":
        formatted_artist_id = f"artist_name_{artist_name}"
    else:
        formatted_artist_id = f"artist_{artist_id}" if artist_id != "0" else "0"
    
    dur_sec = int(attr.get("durationInMillis", 0)) // 1000
    calc_size = int(dur_sec * 256 * 1000 / 8) if dur_sec > 0 else 5000000
    path_str = f"https://music.apple.com/{apple_api.storefront}/song/{s_id}"
    
    return {
        "id": s_id, "mediaFileId": s_id, "songId": s_id, "parent": album_id, "isDir": False,
        "title": attr.get("name", "Unknown Title"), "name": attr.get("name", "Unknown Title"),
        "grouping": "", "work": "", "album": album_name, "albumId": album_id, 
        "artist": artist_name, "artistId": formatted_artist_id,
        "albumArtist": artist_name, "albumArtistId": formatted_artist_id, 
        "trackNumber": attr.get("trackNumber", 1), "discNumber": attr.get("discNumber", 1),
        "coverArt": s_id, "duration": dur_sec, "bitRate": 256, "size": calc_size, "suffix": "m4a", "contentType": "audio/mp4",
        "path": path_str, "type": "music", "year": 2024, "createdAt": "2024-01-01T00:00:00Z", "updatedAt": "2024-01-01T00:00:00Z",
        "playDate": "2024-01-01T00:00:00Z", "play_date": "2024-01-01T00:00:00Z", "created": "2024-01-01T00:00:00Z", "genre": "Apple Music", "plays": 0, "playCount": 0
    }

@router.get("/album")
async def nav_get_albums(
    request: Request, 
    _start: int = Query(0), _end: int = Query(100),
    _sort: str = Query(None), _order: str = Query(None),
    artist_id: str = Query(None), name: str = Query(None), title: str = Query(None)
):
    global GLOBAL_LOCAL_ALBUMS_NAV
    albums = []
    
    try:
        if artist_id and artist_id != "0":
            if artist_id.startswith("artist_name_"):
                query_name = artist_id.replace("artist_name_", "")
                search_res = await apple_api.search(query_name, limit=1, types="artists")
                if search_res.get("artists"):
                    artist_id = f"artist_{search_res['artists'][0]['id']}"
                else:
                    return create_list_response([], 0, _start)

            import re
            real_artist_id = re.sub(r'\D', '', artist_id)
            artist_info = {}
            try: artist_info = await apple_api.get_artist_detail(real_artist_id)
            except: pass
            
            target_name = artist_info.get("attributes", {}).get("name", "") if artist_info else ""
            all_albums_raw = await apple_api.get_artist_albums(real_artist_id, limit=100)
            
            valid_albums = []
            for al in all_albums_raw:
                al_attr = al.get("attributes", {})
                
                if al_attr.get("isSingle", False) == True:
                    continue
                    
                al_artist = al_attr.get("artistName", "")
                if not target_name or target_name.lower() in al_artist.lower():
                    valid_albums.append(al)

            if _start == 0:
                virtual_mv_album_id = f"virtual_mv_album_{real_artist_id}"
                pic_url = apple_api.format_artwork(artist_info.get("attributes", {}).get("artwork", {}).get("url", "")) if artist_info else ""
                if pic_url: NAVIDROME_COVER_MAP[virtual_mv_album_id] = pic_url
                albums.append({
                    "id": virtual_mv_album_id, "name": "★ Videos ★", "title": "★ Videos ★",
                    "artist": target_name or "Artist", "artistId": f"artist_{real_artist_id}", 
                    "albumArtist": target_name or "Artist", "albumArtistId": f"artist_{real_artist_id}", 
                    "coverArt": virtual_mv_album_id, "songCount": 99, "duration": 0, "isDir": True,
                    "year": 2099, "maxYear": 2099, "minYear": 2099, "releaseDate": "2099-01-01",
                    "created": "2099-01-01T00:00:00Z", "createdAt": "2099-01-01T00:00:00Z",
                    "updatedAt": "2099-01-01T00:00:00Z", "description": "Apple Music 精选MV", "genre": "Video"
                })

            if _sort:
                rev = (_order == "DESC")
                try: valid_albums.sort(key=lambda x: x.get("attributes", {}).get("releaseDate", "0"), reverse=rev)
                except: pass

            end_idx = _end if _end > 0 else len(valid_albums)
            sliced = valid_albums[_start:end_idx]
            for al in sliced: 
                albums.append(map_nav_album(al, force_artist_name=target_name or "Unknown Artist", force_artist_id=f"artist_{real_artist_id}"))
                
            dynamic_total = len(valid_albums) + 1 
            return create_list_response(albums, dynamic_total, _start)

        search_query = name or title
        if search_query:
            data = await apple_api.search(search_query, limit=50)
            all_albums = data.get("albums", [])
            end_idx = _end if _end > 0 else len(all_albums)
            sliced = all_albums[_start:end_idx]
            for al in sliced: albums.append(map_nav_album(al))
            return create_list_response(albums, len(all_albums), _start)

        else:
            classical_albums = await apple_api.get_top_100_albums()
            end_idx = _end if _end > 0 else len(classical_albums)
            sliced_items = classical_albums[_start:end_idx]
            
            for al in sliced_items:
                albums.append(map_nav_album(al))
                
            return create_list_response(albums, len(classical_albums), _start)
            
    except Exception as e:
        print(f"[错误] nav_get_albums 异常: {e}")
        return create_list_response([], 0, _start)

@router.get("/album/{id}")
async def nav_get_album_detail(id: str):
    ctx_artist_name = None
    ctx_artist_id = None
    real_id = id
    
    if "_ctx_" in id:
        real_id, artist_id_part = id.split("_ctx_")
        ctx_artist_id = f"artist_{artist_id_part}"
        try:
            art_data = await apple_api.get_artist_detail(artist_id_part)
            ctx_artist_name = art_data.get("attributes", {}).get("name")
        except: pass

    try:
        album_data = await apple_api.get_album_detail(real_id)
        nav_songs = [
            map_nav_song(s, default_album_id=id, force_artist_name=ctx_artist_name, force_artist_id=ctx_artist_id) 
            for s in album_data.get("relationships", {}).get("tracks", {}).get("data", [])
        ]
        al_mapped = map_nav_album(album_data, force_artist_name=ctx_artist_name, force_artist_id=ctx_artist_id)
        
        al_mapped["id"] = id
        al_mapped["songCount"] = len(nav_songs)
        al_mapped["duration"] = sum(s.get("duration", 0) for s in nav_songs)
        al_mapped["songs"] = nav_songs
        return al_mapped
    except Exception as e: 
        print(f"❌ [nav_get_album_detail] 异常: {e}")
        return Response(status_code=404)

@router.get("/artist")
async def nav_search_artists(request: Request, _start: int = Query(0), _end: int = Query(100), name: str = Query(None)):
    artists = []
    try:
        if name:
            data = await apple_api.search(name, limit=50)
            all_artists = data.get("artists", [])
            end_idx = _end if _end > 0 else len(all_artists)
            sliced = all_artists[_start:end_idx]
            
            for art in sliced:
                attr = art.get("attributes", {})
                art_id = str(art["id"])
                pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
                if pic_url: NAVIDROME_COVER_MAP[f"artist_{art_id}"] = pic_url
                artists.append({
                    "id": f"artist_{art_id}", "name": attr.get("name", "Unknown"),
                    "coverArt": f"artist_{art_id}", "artistImageUrl": pic_url, 
                    "smallImageUrl": pic_url, "mediumImageUrl": pic_url, 
                    "largeImageUrl": pic_url, "albumCount": 1
                })
            return create_list_response(artists, len(all_artists), _start)
        return create_list_response([], 0, _start)
    except Exception as e:
        return create_list_response([], 0, _start)

@router.get("/artist/{id}")
async def nav_get_artist_detail(id: str):
    """获取歌手详情：增加健壮性检查"""
    if not id or id == "0": 
        return {"id": id, "name": "未知歌手", "biography": "", "smallImageUrl": "", "mediumImageUrl": "", "largeImageUrl": "", "coverArt": id, "similarArtist": [], "similarArtists": []}
        
    try:
        if id.startswith("artist_name_"):
            query_name = id.replace("artist_name_", "")
            search_res = await apple_api.search(query_name, limit=1, types="artists")
            if search_res.get("artists"):
                real_id = str(search_res["artists"][0]["id"])
                id = f"artist_{real_id}"
            else:
                return {"id": id, "name": query_name, "biography": "未找到该歌手详细信息", "coverArt": id, "similarArtist": [], "similarArtists": []}

        import re
        real_id = re.sub(r'\D', '', id)
        artist_data = await apple_api.get_artist_detail(real_id)
        
        if not artist_data or "attributes" not in artist_data:
            print(f"⚠️ [Navidrome] 无法获取 ID 为 {real_id} 的歌手详情，返回占位符")
            return {"id": id, "name": "未知歌手/ID过时", "biography": "请尝试重新搜索该歌手以更新 ID", "coverArt": id, "similarArtist": [], "similarArtists": []}
        
        attr = artist_data.get("attributes", {})
        name = attr.get("name", "未知歌手")
        
        desc = (
            attr.get("description") or 
            attr.get("editorialNotes", {}).get("standard") or 
            attr.get("editorialNotes", {}).get("short") or 
            "暂无简介"
        )
        desc = re.sub(r'<[^>]+>', '', desc).strip()
        
        pic_url = apple_api.format_artwork(attr.get("artwork", {}).get("url", ""))
        if not pic_url:
            pic_url = await apple_api.get_artist_avatar(real_id)
            if pic_url: NAVIDROME_COVER_MAP[f"artist_{real_id}"] = pic_url
            
        similar_artists = await apple_api.get_similar_artists(real_id, name)
        
        for art in similar_artists:
            if art.get("largeImageUrl"):
                NAVIDROME_COVER_MAP[art["id"]] = art["largeImageUrl"]
            
        return {
            "id": id, "name": name, "biography": desc, "description": desc, 
            "smallImageUrl": pic_url, "mediumImageUrl": pic_url, "largeImageUrl": pic_url, 
            "image_url": pic_url, "coverArt": f"artist_{real_id}", 
            "similarArtist": similar_artists, "similarArtists": similar_artists
        }
    except Exception as e:
        import traceback
        print(f"❌ [Navidrome] 获取歌手详情崩溃！详细堆栈如下:")
        traceback.print_exc()
        return Response(status_code=503, content=f"Internal Server Error: {str(e)}")

@router.get("/artist/{id}/image")
async def nav_get_artist_image(id: str):
    try:
        import re
        real_id = re.sub(r'\D', '', id)
        artist_data = await apple_api.get_artist_detail(real_id)
        pic_url = apple_api.format_artwork(artist_data.get("attributes", {}).get("artwork", {}).get("url", ""))
        if pic_url:
            return RedirectResponse(pic_url)
    except: pass
    return Response(status_code=404)   
    
@router.get("/album/{id}/image")
@router.get("/song/{id}/image")
@router.get("/playlist/{id}/image")
async def nav_get_media_image(id: str, request: Request):
    size = request.query_params.get("size", "300")
    return RedirectResponse(url=f"/rest/getCoverArt?id={id}&size={size}")
        
@router.get("/song")
async def nav_search_songs(
    request: Request, _start: int = Query(0), _end: int = Query(100), 
    title: str = Query(None), name: str = Query(None), 
    album_id: str = Query(None), album_artist_id: str = Query(None),
    artist_id: str = Query(None)
):
    songs = []
    try:
        if album_id and album_id.startswith("virtual_mv_album_"):
            real_artist_id = album_id.replace("virtual_mv_album_", "")
            display_artist = "Artist"
            try:
                art = await apple_api.get_artist_detail(real_artist_id)
                display_artist = art.get("attributes", {}).get("name", "Artist")
            except: pass
            
            songs.append({
                "id": f"vido_{real_artist_id}", "mediaFileId": f"vido_{real_artist_id}", "songId": f"vido_{real_artist_id}",
                "parent": album_id, "isDir": False, "isVideo": True, "hasVideo": True,
                "title": "Apple Music Video", "name": "Apple Music Video",
                "album": "★ Videos ★", "albumId": album_id,
                "artist": display_artist, "artistId": f"artist_{real_artist_id}",
                "albumArtist": display_artist, "albumArtistId": f"artist_{real_artist_id}",
                "trackNumber": 1, "coverArt": f"artist_{real_artist_id}", "duration": 300,
                "contentType": "video/mp4", "suffix": "mp4", "type": "Video", "mediaType": "Video",
                "path": f"https://music.apple.com/cn/artist/{real_artist_id}", "size": 30000000,
                "bitRate": 2000, "year": 2099, "releaseDate": "2099-01-01", "createdAt": "2099-01-01T00:00:00Z"
            })
            return create_list_response(songs, len(songs), _start)

        if album_id:
            real_album_id = album_id.split("_ctx_")[0] if "_ctx_" in album_id else album_id
            ctx_artist_id = f"artist_{album_id.split('_ctx_')[1]}" if "_ctx_" in album_id else None
            
            album_data = await apple_api.get_album_detail(real_album_id)
            
            album_rels = album_data.get("relationships", {})
            album_artist_id_raw = None
            if "artists" in album_rels and album_rels["artists"].get("data"):
                album_artist_id_raw = str(album_rels["artists"]["data"][0].get("id"))
            elif "artistUrl" in album_data.get("attributes", {}):
                import re
                m = re.search(r'/artist/(?:[^/]+/)?(\d+)', album_data["attributes"]["artistUrl"])
                if m: album_artist_id_raw = m.group(1)
            
            fallback_artist_id = ctx_artist_id or (f"artist_{album_artist_id_raw}" if album_artist_id_raw else None)
            
            tracks_data = album_data.get("relationships", {}).get("tracks", {}).get("data", [])
            end_idx = _end if _end > 0 else len(tracks_data)
            sliced_songs = tracks_data[_start:end_idx]
            
            for s in sliced_songs: 
                songs.append(map_nav_song(s, default_album_id=album_id, force_artist_id=fallback_artist_id))
            return create_list_response(songs, len(tracks_data), _start)
                        
        target_artist_id = artist_id if artist_id and artist_id != "0" else album_artist_id
        if target_artist_id and target_artist_id != "0":
            if target_artist_id.startswith("artist_name_"):
                art_name = target_artist_id.replace("artist_name_", "")
            else:
                import re
                real_artist_id = re.sub(r'\D', '', target_artist_id)
                art = await apple_api.get_artist_detail(real_artist_id)
                art_name = art.get("attributes", {}).get("name", "Unknown")
            
            data = await apple_api.search(art_name, limit=50)
            all_songs = data.get("songs", [])
            end_idx = _end if _end > 0 else len(all_songs)
            sliced_songs = all_songs[_start:end_idx]
            for s in sliced_songs: songs.append(map_nav_song(s))
            return create_list_response(songs, len(all_songs), _start)
            
        search_query = title or name
        if search_query:
            data = await apple_api.search(search_query, limit=50)
            all_songs = data.get("songs", [])
            end_idx = _end if _end > 0 else len(all_songs)
            sliced_songs = all_songs[_start:end_idx]
            for s in sliced_songs: songs.append(map_nav_song(s))
            return create_list_response(songs, len(all_songs), _start)
            
        return create_list_response([], 0, _start)
    except Exception as e:
        print(f"❌ [Navidrome] 获取单曲/搜索失败: {e}")
        return create_list_response([], 0, _start)

@router.get("/song/{id}")
async def nav_get_song_detail(id: str):
    if id.startswith("mv_") or id.startswith("vido_"):
        real_mv_id = id.replace("mv_", "").replace("vido_", "")
        display_artist, title_str = "Unknown Artist", f"官方影像_{real_mv_id}"
        return {
            "id": id, "mediaFileId": id, "songId": id,
            "isDir": False, "isVideo": True, "hasVideo": True,
            "title": title_str, "name": title_str,
            "album": "★ Videos ★", "artist": display_artist,
            "coverArt": id, "duration": 300, 
            "contentType": "video/mp4", "suffix": "mp4",
            "type": "Video", "mediaType": "Video",
            "path": f"https://music.apple.com/cn/song/{real_mv_id}",
            "size": 50000000, "bitRate": 2000
        }

    try:
        real_id = re.sub(r'\D', '', id)
        if not real_id: return Response(status_code=404)
        
        song_data = await apple_api.get_song_detail(real_id)
        song_obj = map_nav_song(song_data)
            
        try:
            lrc_text = await apple_api.get_lyrics(real_id)
            if lrc_text:
                pattern = re.compile(r'\[(\d{2,}):(\d{2})(?:\.(\d{1,3}))?\](.*)')
                merged_lines = []
                for line in lrc_text.split('\n'):
                    match = pattern.match(line)
                    if match:
                        m, s, ms, val = match.groups()
                        ms_val = int((ms or "00").ljust(3, '0')[:3])
                        time_ms = int(m) * 60000 + int(s) * 1000 + ms_val
                        merged_lines.append({"start": time_ms, "value": val.strip()})
                
                if merged_lines:
                    song_obj["lyrics"] = json.dumps([{"lang": "Apple Music", "line": merged_lines}], ensure_ascii=False)
        except Exception as e:
            print(f"[歌词解析警告] ID {real_id} 歌词解析跳过: {e}")
                
        return song_obj
            
    except Exception as e:
        print(f"❌ [nav_get_song_detail 404 错误] ID {id} 获取失败: {e}")
        return Response(status_code=404)

@router.get("/song/{id}/file")
async def nav_play_song(request: Request, background_tasks: BackgroundTasks, id: str): 
    import shutil
    if id.startswith("vido_") or id.startswith("nmv_") or id.startswith("mv_"): return Response(status_code=404)
    try:
        real_id = re.sub(r'\D', '', str(id))
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
    
@router.get("/user")
async def nav_get_users(request: Request, _start: int = Query(0)):
    current_user = get_current_user(request)
    user_obj = {
        "id": current_user, 
        "username": current_user, 
        "name": current_user, 
        "isAdmin": True
    }
    return create_list_response([user_obj], 1, _start)

@router.get("/user/{id}")
async def nav_get_user_detail(id: str, request: Request):
    return {"id": id, "username": id, "name": id, "isAdmin": True}

def format_nav_playlist(pl: dict, current_user: str):
    if not pl: return {}
    
    pl_id_str = str(pl.get("id", ""))
    tracks = db.get_playlist_tracks(pl_id_str)
    cover_id = pl_id_str 
    
    if tracks:
        for track in reversed(tracks):
            cid = str(track.get("coverArt", "0"))
            if cid and cid != "0":
                cover_id = cid
                break

    real_owner = pl.get("owner") or current_user

    c_time = pl.get("created", time.time())
    u_time = pl.get("updated", time.time())
    s_count = pl.get("song_count", 0)
    dur = pl.get("duration", 0)

    return {
        "id": pl_id_str,      
        "name": pl.get("name", "Unknown Playlist"),
        "comment": "",
        "owner": {"id": real_owner, "username": real_owner},
        "ownerName": real_owner,                             
        "ownerId": real_owner,    
        "isOwner": True,
        "editable": True,
        "allowedUser": True,
        "permission": {"canEdit": True, "canDelete": True, "canShare": True, "canDownload": True},
        "public": False,          
        "songCount": int(s_count),
        "duration": int(dur),
        "created": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(c_time)),
        "updatedAt": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(u_time)),
        "coverArt": cover_id,
        "rules": "",
        "sync": False
    }

@router.get("/playlist")
async def nav_get_playlists_list(request: Request, _start: int = Query(0), _end: int = Query(0)):
    current_user = get_current_user(request)
    pls = db.get_playlists()
    formatted = [format_nav_playlist(p, current_user) for p in pls]
    
    end_idx = _end if _end > 0 else len(formatted)
    sliced = formatted[_start:end_idx]
    
    return create_list_response(sliced, len(formatted), _start)


@router.get("/playlist/{id}")
async def nav_get_playlist_detail(request: Request, id: str):
    current_user = get_current_user(request)
    pl = db.get_playlist(id)
    if not pl: return Response(status_code=404)
    return format_nav_playlist(pl, current_user)    
    

@router.get("/playlist/{id}/tracks")
async def nav_get_playlist_tracks(id: str, _start: int = Query(0)):
    tracks = db.get_playlist_tracks(id)
    
    formatted_tracks = []
    if tracks:
        for t in tracks:
            track_obj = dict(t)
            
            artist = track_obj.get("artist", "Unknown")
            album = track_obj.get("album", "Unknown")
            title = track_obj.get("title", "Unknown")
            song_id = str(track_obj.get("id", ""))
            
            track_obj["path"] = f"https://music.apple.com/{apple_api.storefront}/song/{song_id}"
            track_obj["mediaFileId"] = song_id
            track_obj["songId"] = song_id
            track_obj["playlistId"] = str(id)
            track_obj["isOwner"] = True
            track_obj["editable"] = True
            track_obj["canEdit"] = True
            track_obj["canDelete"] = True
            track_obj["allowedUser"] = True
            track_obj["coverArt"] = song_id 
                    
            formatted_tracks.append(track_obj)
            
    return create_list_response(formatted_tracks, len(formatted_tracks), _start)

@router.post("/playlist")
async def nav_create_playlist(request: Request):
    try: 
        payload = await request.json()
    except: 
        payload = {}
        
    name = payload.get("name") or request.query_params.get("name") or "New Playlist"
    current_user = get_current_user(request)
    
    pl_id = db.create_playlist(name, owner=current_user) 
    pl = db.get_playlist(pl_id)
    
    if not pl:
        return JSONResponse(status_code=500, content={"error": "Database creation failed"})
        
    return format_nav_playlist(pl, current_user)

@router.put("/playlist/{id}")
async def nav_update_playlist(request: Request, id: str):
    try: payload = await request.json()
    except: payload = {}
    
    current_user = get_current_user(request)
    if "name" in payload:
        db.update_playlist_name(id, payload["name"])
    return format_nav_playlist(db.get_playlist(id), current_user)

@router.delete("/playlist/{id}")
async def nav_delete_playlist(id: str):
    db.delete_playlist(id)
    return JSONResponse(content={}) 

@router.delete("/playlist/{id}/tracks")
async def nav_remove_track_from_playlist(request: Request, id: str):
    payload = {}
    try:
        payload = await request.json()
    except: pass 
        
    indexes = payload.get("indexes", [])
    
    target_song_id = request.query_params.get("id")
    if target_song_id:
        tracks = db.get_playlist_tracks(id)
        for idx, t in enumerate(tracks):
            if str(t.get("id")) == str(target_song_id):
                indexes.append(idx)
                break 
                
    if indexes:
        for idx in sorted(list(set(indexes)), reverse=True):
            db.remove_track_from_playlist(id, idx)
            
    return JSONResponse(content={})


@router.post("/playlist/{id}/tracks")
async def nav_add_tracks_to_playlist(request: Request, id: str):
    raw_body = await request.body()
    body_str = raw_body.decode('utf-8', errors='ignore')
    
    payload = {}
    song_ids = []

    if body_str:
        try:
            payload = json.loads(body_str)
            if isinstance(payload, dict):
                for key in ["ids", "songIds", "songId", "mediaFileIds", "itemIds", "trackIds", "tracks"]:
                    if key in payload:
                        song_ids = payload[key]
                        break
            elif isinstance(payload, list):
                song_ids = payload
        except: pass

    if not song_ids:
        for key in ["id", "songId", "ids", "mediaFileId", "itemId"]:
            val = request.query_params.getlist(key)
            if val:
                song_ids = val
                break

    if isinstance(song_ids, str) or isinstance(song_ids, int):
        song_ids = [song_ids]

    if song_ids:
        real_sids = [re.sub(r'\D', '', str(sid)) for sid in song_ids if re.sub(r'\D', '', str(sid))]

        tracks_to_add = []
        if real_sids:
            try:
                async def fetch_and_map(sid):
                    try:
                        s_data = await apple_api.get_song_detail(sid)
                        return map_nav_song(s_data)
                    except: return None

                tasks = [fetch_and_map(sid) for sid in real_sids]
                results = await asyncio.gather(*tasks)
                tracks_to_add = [r for r in results if r]
                    
            except Exception as e:
                print(f"❌ [报错] 批量获取歌曲详情失败: {e}")

        if tracks_to_add:
            db.add_tracks_to_playlist(id, tracks_to_add)
            print(f"✅ 向歌单增加了 {len(tracks_to_add)} 首歌曲！")

    return Response(status_code=200)

@router.get("/video.html")
async def serve_video_html():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base_dir, "static", "video.html"))

@router.get("/player-feishin.js")
async def serve_player_feishin_js():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(base_dir, "static", "player-feishin.js"))

@router.api_route("/custom_lrc.view", methods=["GET", "POST"])
@router.api_route("/netease_proxy/rest/custom_lrc.view", methods=["GET", "POST"])
async def custom_lrc_api(request: Request):
    from urllib.parse import unquote
    from difflib import SequenceMatcher

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

    target_title = clean(title)
    target_artist = clean(artist)

    def similarity(a, b): return SequenceMatcher(None, a, b).ratio()

    try:
        search_res = await apple_api.search(f"{title} {artist}", limit=20)
        candidates = search_res.get("songs", [])
        
        if not candidates: 
            return Response(content=f"[00:00.00] 未找到歌曲: {title}", media_type="text/plain; charset=utf-8")

        best_song = None
        best_score = 0
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
            if score > best_score: best_score = score; best_song = s

        if not best_song or best_score < 0.55:
            return Response(content=f"[00:00.00] 匹配失败: {title}", media_type="text/plain; charset=utf-8")

        lrc_text = await apple_api.get_lyrics(str(best_song["id"]))
        if not lrc_text: return Response(content="[00:00.00] 暂无歌词", media_type="text/plain; charset=utf-8")

        return Response(content=lrc_text, media_type="text/plain; charset=utf-8")

    except Exception as e:
        print("歌词API异常:", str(e))
        return Response(content="[00:00.00] 歌词服务异常", media_type="text/plain; charset=utf-8")