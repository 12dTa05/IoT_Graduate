# signaling_server.py
import asyncio, json
from aiohttp import web, WSMsgType

# Lưu kết nối WS theo room
ROOMS = {}
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    room = request.query.get("room", "demo")
    peers = ROOMS.setdefault(room, set())
    peers.add(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Broadcast cho các peer khác cùng room
                data = msg.data
                for p in list(peers):
                    if p is not ws:
                        await p.send_str(data)
            elif msg.type == WSMsgType.ERROR:
                print("ws error:", ws.exception())
    finally:
        peers.discard(ws)
        if not peers:
            ROOMS.pop(room, None)
    return ws

async def index(request):
    return web.FileResponse('./index.html')

app = web.Application()
app.router.add_get('/', index)
app.router.add_get('/ws', ws_handler)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)


# signaling_server.py
# import asyncio, json, os
# from aiohttp import web, WSMsgType

# HLS_DIR = "/var/www/hls"

# # 1 pub (Jetson) + 1 sub (Recorder) ở room để nhận luồng.
# # Xem trên web qua HLS do recorder xuất ra (nhiều người xem cùng lúc).
# ROOMS = {}  # {room: {"pub": ws_or_None, "sub": ws_or_None}}

# async def ws_handler(request):
#     ws = web.WebSocketResponse()
#     await ws.prepare(request)
#     room = request.query.get("room", "demo")
#     role = request.query.get("role", "sub")  # 'pub' or 'sub'

#     state = ROOMS.setdefault(room, {"pub": None, "sub": None})
#     old = state.get(role)
#     if old and not old.closed:
#         await old.close()
#     state[role] = ws
#     print(f"[SRV] {role} joined room={room}")

#     peer_role = "sub" if role == "pub" else "pub"
#     try:
#         async for msg in ws:
#             if msg.type == WSMsgType.TEXT:
#                 peer = state.get(peer_role)
#                 if peer and not peer.closed:
#                     await peer.send_str(msg.data)
#             elif msg.type == WSMsgType.ERROR:
#                 print("ws error:", ws.exception())
#     finally:
#         if state.get(role) is ws:
#             state[role] = None
#         print(f"[SRV] {role} left room={room}")
#     return ws

# async def index(request):
#     # Trang đơn giản: link vào HLS player
#     return web.Response(text="OK. HLS at /hls/stream.m3u8, Player at /hls.html")

# # Player HLS đơn giản
# async def hls_html(request):
#     html = f"""<!doctype html>
# <html><head><meta charset="utf-8"><title>HLS Player</title>
# <style>body{{margin:0;background:#111;color:#ddd;display:grid;place-items:center;height:100vh}}
# video{{width:90vw;max-width:1280px;background:#000}}</style></head>
# <body>
# <video id="v" controls autoplay playsinline></video>
# <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
# <script>
# const url = location.origin.replace('http://','http://') + '/hls/stream.m3u8';
# const v = document.getElementById('v');
# if (Hls.isSupported()){{ const h=new Hls(); h.loadSource(url); h.attachMedia(v); }}
# else if (v.canPlayType('application/vnd.apple.mpegurl')){{ v.src = url; }}
# </script>
# </body></html>"""
#     return web.Response(text=html, content_type="text/html")

# app = web.Application()
# app.router.add_get('/', index)
# app.router.add_get('/ws', ws_handler)
# app.router.add_get('/hls.html', hls_html)
# app.router.add_static('/hls', HLS_DIR)   # phục vụ file HLS

# if __name__ == "__main__":
#     os.makedirs(HLS_DIR, exist_ok=True)
#     web.run_app(app, host="0.0.0.0", port=8080)