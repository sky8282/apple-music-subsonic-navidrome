import re
import os
import json
import time
import httpx
import requests
import urllib.parse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from cachetools import LRUCache
from typing import Dict, List, Any, Optional

class AppleMusicAPI:
    def __init__(self, storefront: str = "cn"):
        self.storefront = storefront
        self.base_url = "https://api.music.apple.com/v1"
        self.token_cache_file = "apple_token_cache.json"
        self.token = ""
        self.token_expires = 0
        self.lyric_cache = LRUCache(maxsize=100)
        self._ensure_valid_token()

    def _ensure_valid_token(self):
        current_time = time.time()
        if self.token and current_time < self.token_expires:
            return self.token

        if os.path.exists(self.token_cache_file):
            try:
                with open(self.token_cache_file, 'r') as f:
                    cache_data = json.load(f)
                if cache_data.get("token") and current_time < cache_data.get("expires_at", 0):
                    self.token = cache_data["token"]
                    self.token_expires = cache_data["expires_at"]
                    return self.token
            except Exception:
                pass

        print("🔄 正在自动从 Apple Music 刷新 Bearer Token...")
        try:
            r = requests.get("https://music.apple.com", timeout=15)
            r.raise_for_status()
            
            import re
            js_path_match = re.search(r'src="(/[^"]*index[^"]*\.js)"', r.text)
            if not js_path_match: raise Exception("未找到包含Token的JS文件")

            js_url = "https://music.apple.com" + js_path_match.group(1)
            js_content_res = requests.get(js_url, timeout=15)
            js_content_res.raise_for_status()
            
            token_match = re.search(r'"(eyJhbGciOi[^"]+)"', js_content_res.text)
            if not token_match: raise Exception("JS文件中未匹配到Token")

            new_token = token_match.group(1)
            new_expires = current_time + 86400 * 7

            self.token = new_token
            self.token_expires = new_expires

            try:
                with open(self.token_cache_file, 'w') as f:
                    json.dump({"token": new_token, "expires_at": new_expires}, f)
            except Exception as e:
                print(f"⚠️ Token 缓存文件保存到根目录失败 (暂用内存维持运行): {e}")

            print("✅ 成功获取并缓存全新 Apple Music Token！")
            return self.token
            
        except Exception as e:
            print(f"❌ 自动获取 Token 失败: {e}")
            return self.token

    def _get_media_user_token(self, target_storefront: str) -> str:
        try:
            import yaml
            import os
            
            config_path = "config.yaml" 
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    for acc in config.get('accounts', []):
                        if acc.get('storefront', '').lower() == target_storefront.lower():
                            return acc.get('media-user-token', '')
        except ImportError:
            print("⚠️ [DEBUG] 未安装 pyyaml，无法解析 config.yaml, 请运行: pip install pyyaml")
        except Exception as e:
            print(f"⚠️ [DEBUG] 读取 config.yaml 提取 token 失败: {e}")
        return ""
    
    
    @property
    def headers(self):
        self._ensure_valid_token()
        
        h = {
            "Origin": "https://music.apple.com",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Default-User-Language": "zh-CN"
        }
        
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
            
        return h

    def _format_artwork(self, url: str) -> str:
        if not url: return ""
        return url.replace("{w}", "300").replace("{h}", "300").replace("{f}", "jpg")

    def format_artwork(self, url: str) -> str:
        return self._format_artwork(url)

    async def search(self, keyword: str, limit: int = 20, types: str = "songs,albums,artists") -> Dict[str, Any]:
        safe_limit = min(limit, 25)
        url = f"{self.base_url}/catalog/{self.storefront}/search"
        
        params = {
            "term": keyword,
            "limit": safe_limit,
            "types": types,
            "include": "artists,albums,composers", 
            "extend": "artists,composers",
            "l": "zh-CN"
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self.headers, params=params)
            if resp.status_code != 200:
                return {"songs": [], "albums": [], "artists": []}
            
            data = resp.json().get("results", {})
            
            return {
                "songs": data.get("songs", {}).get("data", []),
                "albums": data.get("albums", {}).get("data", []),
                "artists": data.get("artists", {}).get("data", [])
            }

    async def get_song_detail(self, song_id: str) -> dict:
        url = f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/songs/{song_id}?include=artists,albums,composers&extend=artists,composers"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code != 200: raise Exception("Song not found")
            return resp.json().get("data", [{}])[0]

    async def get_album_detail(self, album_id: str) -> dict:
        url = f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/albums/{album_id}?include=artists,tracks,composers&extend=tracks:composers"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code != 200: raise Exception("Album not found")
            return resp.json().get("data", [{}])[0]

    async def get_songs_by_ids(self, song_ids: list) -> list:
        if not song_ids: return []
        try:
            ids_str = ",".join(song_ids)
            
            url = f"https://amp-api.music.apple.com/v1/catalog/{self.storefront}/songs?ids={ids_str}&include=artists,albums,composers&extend=artists,composers"
            
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=self.headers, timeout=15.0)
                if resp.status_code == 200:
                    return resp.json().get("data", [])
        except Exception as e:
            print(f"❌ [API] 批量获取曲目元数据失败: {e}")
        return []
              
    async def get_artist_detail(self, artist_id: str) -> dict:
        url = f"{self.base_url}/catalog/{self.storefront}/artists/{artist_id}"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url, headers=self.headers)
                if resp.status_code != 200:
                    print(f"⚠️ [API] 歌手 ID {artist_id} 未找到，尝试网页检索...")
                    web_url = f"https://music.apple.com/{self.storefront}/artist/{artist_id}"
                    web_resp = await client.get(web_url, headers={'User-Agent': 'Mozilla/5.0'})
                    if web_resp.status_code == 200:
                        import re
                        m_id = re.search(r'apple-music-artist/(\d+)', web_resp.text)
                        if m_id and m_id.group(1) != artist_id:
                            print(f"🔄 发现新 ID: {m_id.group(1)}，重新获取...")
                            return await self.get_artist_detail(m_id.group(1))
                    return {}

                data = resp.json().get("data", [])
                artist_node = data[0] if data else {}
                
                if artist_node:
                    attr = artist_node.setdefault("attributes", {})
                    import re, html, json
                    web_url = f"https://music.apple.com/{self.storefront}/artist/{artist_id}"
                    web_resp = await client.get(web_url, headers={'User-Agent': 'Mozilla/5.0'})
                    if web_resp.status_code == 200:
                        m = re.search(r'"description":"([^"\\]*(?:\\.[^"\\]*)*)"', web_resp.text)
                        if m:
                            try:
                                clean_desc = json.loads(f'"{m.group(1)}"')
                                attr["description"] = html.unescape(clean_desc)
                            except: pass
                return artist_node
            except Exception as e:
                print(f"❌ [get_artist_detail] 内部异常: {e}")
                return {}

    async def get_artist_albums(self, artist_id: str, limit: int = 100, offset: int = 0) -> list:
        url = f"{self.base_url}/catalog/{self.storefront}/artists/{artist_id}/albums"
        params = {"limit": limit, "offset": offset}
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=self.headers, params=params)
            if resp.status_code != 200: return []
            return resp.json().get("data", [])

    async def get_lyrics(self, song_id: str) -> str:
        if song_id in self.lyric_cache:
            return self.lyric_cache[song_id]

        storefronts = [self.storefront]
        if self.storefront != "us": 
            storefronts.append("us")
            
        lrc_types = ["syllable-lyrics", "lyrics"]
            
        async with httpx.AsyncClient(timeout=10.0) as client:
            for sf in storefronts:
                my_media_user_token = self._get_media_user_token(sf)
                cookies = {}
                if my_media_user_token and len(my_media_user_token) > 50:
                    cookies["media-user-token"] = my_media_user_token

                for ltype in lrc_types:
                    url = f"https://amp-api.music.apple.com/v1/catalog/{sf}/songs/{song_id}/{ltype}"
                    params = {
                        "l": "zh-Hans-CN,zh-Hans;q=0.8,zh-Hant;q=0.6,en-US;q=0.4",
                        "extend": "ttmlLocalizations"
                    }    
                    
                    try:
                        resp = await client.get(url, headers=self.headers, params=params, cookies=cookies)
                        if resp.status_code == 200:
                            data = resp.json().get("data", [])
                            if not data: continue
                                
                            attrs = data[0].get("attributes", {})
                            ttml = attrs.get("ttmlLocalizations") or attrs.get("ttml", "")
                            
                            if not ttml: continue
                                
                            final_lrc = self._parse_ttml_to_lrc(ttml)
                            
                            if final_lrc:
                                self.lyric_cache[song_id] = final_lrc
                                
                            return final_lrc
                            
                    except Exception:
                        continue
                        
        return ""

    def _parse_ttml_to_lrc(self, ttml: str) -> str:
        try:
            import html
            import re
            lines = []
            
            p_pattern = r'<p\s+([^>]*)>(.*?)</p>'
            matches = list(re.finditer(p_pattern, ttml, re.DOTALL | re.IGNORECASE))
            
            if matches:
                for match in matches:
                    attrs = match.group(1)
                    content = match.group(2)
                    
                    begin_match = re.search(r'begin="([^"]+)"', attrs, re.IGNORECASE)
                    if not begin_match: continue
                    timestamp = self._convert_time(begin_match.group(1))
                    
                    original_text = re.sub(r'<[^>]+>', '', content)
                    original_text = html.unescape(original_text)
                    original_text = " ".join(original_text.split())
                    
                    trans_text = ""
                    key_match = re.search(r'itunes:key="([^"]+)"', attrs, re.IGNORECASE)
                    if key_match:
                        itunes_key = key_match.group(1)
                        trans_pattern = rf'<text[^>]*for="{itunes_key}"[^>]*>(.*?)</text>'
                        trans_match = re.search(trans_pattern, ttml, re.DOTALL | re.IGNORECASE)
                        
                        if trans_match:
                            raw_trans = trans_match.group(1)
                            raw_trans = re.sub(r'<[^>]+>', '', raw_trans)
                            raw_trans = html.unescape(raw_trans)
                            trans_text = " ".join(raw_trans.split())
                            
                    if trans_text:
                        lines.append(f"{timestamp}{trans_text}")
                        
                    if original_text:
                        lines.append(f"{timestamp}{original_text}")
                                
            return "\n".join(lines)
            
        except Exception:
            return ""

    async def get_similar_artists(self, artist_id: str, name: str = "") -> list:
        storefronts_to_try = ["us", self.storefront] if self.storefront != "us" else ["us"]

        for sf in storefronts_to_try:
            api_url = f"{self.base_url}/catalog/{sf}/artists/{artist_id}?include=similar-artists,related-artists"
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(api_url, headers=self.headers)
                    if resp.status_code == 200:
                        data = resp.json().get("data", [])
                        if data:
                            rels = data[0].get("relationships", {})
                            related_data = rels.get("similar-artists", {}).get("data") or rels.get("related-artists", {}).get("data", [])
                            if related_data:
                                results = []
                                for art in related_data:
                                    art_id = str(art["id"])
                                    pic_url = self.format_artwork(art.get("attributes", {}).get("artwork", {}).get("url", ""))
                                    results.append({
                                        "id": f"artist_{art_id}", "name": art.get("attributes", {}).get("name", "Unknown"),
                                        "coverArt": f"artist_{art_id}", "artistImageUrl": pic_url,
                                        "smallImageUrl": pic_url, "mediumImageUrl": pic_url, "largeImageUrl": pic_url,
                                        "albumCount": 1
                                    })
                                return results
            except Exception:
                pass

        import urllib.parse
        import re
        from bs4 import BeautifulSoup
        
        name_slug = urllib.parse.quote(name.lower().replace(' ', '-')) if name else "artist"
        
        url = f"https://music.apple.com/us/artist/{name_slug}/{artist_id}/see-all?section=similar-artists"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cookie': 'geo=US' 
        }
        
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url, headers=headers)
                
                url_str = str(resp.url)
                if '/new' in url_str or '/browse' in url_str or '/room/' in url_str or '/grouping/' in url_str:
                    return []
                
                soup = BeautifulSoup(resp.text, 'html.parser')
                li_items = soup.find_all('li', class_='grid-item')
                
                related = []
                for li in li_items:
                    target_href = ""
                    for a in li.find_all('a'):
                        link = a.get('href', '')
                        if link and ('/artist/' in link or re.search(r'\d{5,}', link)):
                            target_href = link
                            break
                            
                    if not target_href: 
                        continue
                        
                    href_clean = target_href.split('?')[0].rstrip('/')
                    rel_id = href_clean.split('/')[-1]
                    
                    if not rel_id.isdigit():
                        m = re.search(r'/artist/[^/]+/(\d+)', target_href)
                        if m: 
                            rel_id = m.group(1)
                        else:
                            m2 = re.search(r'/(\d{5,})', target_href)
                            if m2: rel_id = m2.group(1)
                            else: continue
                            
                    rel_name = "未知"
                    title_elem = li.find(attrs={'data-testid': re.compile(r'title', re.I)})
                    if title_elem:
                        rel_name = title_elem.text.strip()
                    else:
                        for a in li.find_all('a'):
                            if a.get('aria-label'):
                                rel_name = a.get('aria-label').strip()
                                break
                        if rel_name == "未知":
                            rel_name = li.text.replace('\n', ' ').strip()

                    img_url = ""
                    source = li.find('source')
                    if source and source.get('srcset'):
                        img_url = source.get('srcset').split(',')[0].split()[0]
                    else:
                        img = li.find('img')
                        if img and img.get('src'):
                            img_url = img.get('src')
                    
                    if img_url:
                        img_url = re.sub(r'/\d+x\d+[a-zA-Z0-9-]*\.[a-zA-Z]+', '/300x300bb.jpg', img_url)
                        
                    related.append({
                        "id": f"artist_{rel_id}", "name": rel_name,
                        "coverArt": f"artist_{rel_id}", "artistImageUrl": img_url,
                        "smallImageUrl": img_url, "mediumImageUrl": img_url, "largeImageUrl": img_url,
                        "albumCount": 1
                    })
                
                return related
            except Exception:
                return []

    async def get_top_100_albums(self) -> list:
        url = "https://classical.music.apple.com/api/classical/v7/query/view/us/section/6503959519/albums"
        headers = {
            "accept": "*/*", "auth-storefront": "us",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        params = {"platform": "web", "limit": 100, "l": "zh-CN"}
        
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    items = resp.json().get("firstPage", {}).get("items", [])
                    album_ids = [str(item.get("id")) for item in items if item.get("id")]
                    
                    if album_ids:
                        ids_str = ",".join(album_ids)
                        catalog_url = f"{self.base_url}/catalog/{self.storefront}/albums"
                        cat_resp = await client.get(catalog_url, headers=self.headers, params={"ids": ids_str})
                        if cat_resp.status_code == 200:
                            return cat_resp.json().get("data", [])
            except Exception as e:
                print(f"[Top 100] 批量获取真实数据失败: {e}")
        return []

    def _parse_songs(self, data: List[dict]) -> List[dict]:
        results = []
        for item in data:
            attr = item.get("attributes", {})
            results.append({
                "id": item.get("id"),
                "name": attr.get("name"),
                "artist": attr.get("artistName"),
                "album": attr.get("albumName"),
                "coverArt": self._format_artwork(attr.get("artwork", {}).get("url", "")),
                "duration": int(attr.get("durationInMillis", 0) // 1000),
                "trackNumber": attr.get("trackNumber"),
                "discNumber": attr.get("discNumber"),
                "releaseDate": attr.get("releaseDate")
            })
        return results

    def _parse_albums(self, data: List[dict]) -> List[dict]:
        results = []
        for item in data:
            attr = item.get("attributes", {})
            results.append({
                "id": item.get("id"),
                "name": attr.get("name"),
                "artist": attr.get("artistName"),
                "coverArt": self._format_artwork(attr.get("artwork", {}).get("url", "")),
                "year": attr.get("releaseDate", "")[:4],
                "trackCount": attr.get("trackCount")
            })
        return results

    def _parse_artists(self, data: List[dict]) -> List[dict]:
        results = []
        for item in data:
            attr = item.get("attributes", {})
            results.append({
                "id": item.get("id"),
                "name": attr.get("name"),
                "coverArt": self._format_artwork(attr.get("artwork", {}).get("url", "")),
                "genre": attr.get("genreNames", [""])[0]
            })
        return results

    def _convert_time(self, time_str: str) -> str:
        """将 Apple 的 00:00:00.000 或 0.00s 转换为 [mm:ss.xx] LRC 标准格式"""
        time_str = time_str.replace("s", "")
        try:
            if ":" in time_str:
                parts = time_str.split(":")
                s = float(parts[-1])
                m = float(parts[-2])
                if len(parts) > 2:
                    m += float(parts[-3]) * 60 
            else:
                total_seconds = float(time_str)
                m = total_seconds // 60
                s = total_seconds % 60
            return f"[{int(m):02d}:{s:05.2f}]"
        except:
            return "[00:00.00]"

def _get_default_storefront(default_sf: str = "cn") -> str:
    import os
    try:
        import yaml
        config_path = "config.yaml" 
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
                accounts = config.get('accounts', [])
                if accounts and isinstance(accounts, list) and len(accounts) > 0:
                    sf = accounts[0].get('storefront')
                    if sf:
                        return sf.strip().lower()
    except Exception as e:
        print(f"⚠️ 读取 config.yaml 提取 storefront 失败，回退到默认区域 {default_sf}: {e}")
        
    return default_sf

apple_api = AppleMusicAPI(storefront=_get_default_storefront())
