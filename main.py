import os
import sys
import time
import uvicorn

def start_apple_music_bridge():
    """启动 Apple Music Subsonic & Navidrome 桥接服务"""
    try:
        print("\n" + "="*60)
        print("🍎 Apple Music Bridge 启动中...")
        print("="*60)
        print(f"📡 Subsonic & Navidrome 桥接地址: http://0.0.0.0:8800")
        print(f"📁 音轨临时缓存目录: ./temp_cache")
        print("\n📚     挂载服务:")
        print(f"  ├─ 🎵 Apple Music API ")
        print(f"  ├─ 🔐 必须安装苹果音乐解密程序 (并发锁: 2)")
        print(f"  ├─ 📡 Subsonic & Navidrome 协议格式")
        print(f"  └─ 📺 音频代理转发")
        print("="*60)
        print(f"⏰ 启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("🌟 服务已就绪...\n")

        uvicorn.run("subsonic:app", host="0.0.0.0", port=8800, reload=False)
        
    except KeyboardInterrupt:
        print("\n\n👋 服务已停止")
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        sys.exit(1)

if __name__ == '__main__':
    start_apple_music_bridge()