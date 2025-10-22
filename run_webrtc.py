# run_webrtc.py (bản mới)
#!/usr/bin/env python3
import sys, argparse, asyncio, gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GLib
import speedflow.settings as S
from speedflow.homography import load_points, ViewTransformer
from speedflow.probes import SpeedProbe
from speedflow.config_txt import load_kv_txt
from speedflow.pipeline_webrtc import build_webrtc_pipeline

import json, websockets
from gi.repository import GstWebRTC, GstSdp

class WebRTCSession:
    def __init__(self, webrtc, ws_uri):
        self.webrtc = webrtc
        self.ws_uri = ws_uri
        self.ws = None
        self.loop = None
        self._closing = False

        self.webrtc.connect("on-negotiation-needed", self.on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self.on_ice_candidate)

    async def _ws_connect(self):
        while not self._closing:
            try:
                self.ws = await websockets.connect(self.ws_uri)
                print("[JETSON] WS connected", self.ws_uri)
                return
            except Exception as e:
                print("[JETSON] WS connect failed, retrying...", e)
                await asyncio.sleep(1.2)

    async def connect(self):
        self.loop = asyncio.get_running_loop()
        await self._ws_connect()
        asyncio.create_task(self._recv_loop())

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "answer":
                    res, sdpmsg = GstSdp.SDPMessage.new_from_text(msg["sdp"])
                    if res != GstSdp.SDPResult.OK:
                        print("[JETSON] SDP parse failed"); continue
                    answer = GstWebRTC.WebRTCSessionDescription.new(
                        GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
                    )
                    self.webrtc.emit("set-remote-description", answer, None)
                    print("[JETSON] set-remote-description done")
                elif t == "ice":
                    cand = msg["candidate"]["candidate"]
                    mline = int(msg["candidate"]["sdpMLineIndex"])
                    self.webrtc.emit("add-ice-candidate", mline, cand)
        except Exception as e:
            print("[JETSON] WS recv loop ended:", e)
            await self._handle_ws_drop()

    async def _handle_ws_drop(self):
        if self._closing: return
        try:
            if self.ws: await self.ws.close()
        except: pass
        self.ws = None
        print("[JETSON] Reconnecting WS...")
        await asyncio.sleep(0.8)
        await self._ws_connect()
        await asyncio.sleep(0.2)
        self.on_negotiation_needed(self.webrtc)

    def on_negotiation_needed(self, element):
        print("[JETSON] on-negotiation-needed")
        p = Gst.Promise.new_with_change_func(self._on_offer_created, element, None)
        self.webrtc.emit("create-offer", None, p)

    def _on_offer_created(self, promise, element, _):
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        self.webrtc.emit("set-local-description", offer, None)
        text = offer.sdp.as_text()
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(
            {"type":"offer", "sdp": text})), self.loop)

    def on_ice_candidate(self, element, mline, candidate):
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps({
            "type":"ice","candidate":{"candidate":candidate,"sdpMLineIndex":int(mline)}
        })), self.loop)

    def send_json_threadsafe(self, data: dict):
        if not self.ws: return
        try:
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(data)), self.loop)
        except Exception as e:
            print("[JETSON] publish JSON failed:", e)

async def async_main():
    parser = argparse.ArgumentParser(description="DeepStream WebRTC runner (load TXT config)")
    parser.add_argument("rtsp_or_file", help="RTSP url hoặc đường dẫn file (/path, hoặc file:///...)")
    parser.add_argument("--server", default="192.168.0.158", help="IP server WS signaling")
    parser.add_argument("--room", default="demo", help="room name")
    parser.add_argument("--cfg", required=True, help="đường dẫn file TXT chứa ANALYTICS_CFG, HOMO_YML, VIDEO_FPS")
    args = parser.parse_args()
    kv = load_kv_txt(args.cfg)
    S.ANALYTICS_CFG = kv["ANALYTICS_CFG"]
    S.HOMO_YML      = kv["HOMO_YML"]
    S.VIDEO_FPS     = kv["VIDEO_FPS"]
    S.MUX_WIDTH  = int(kv.get("MUX_WIDTH", 1280))
    S.MUX_HEIGHT = int(kv.get("MUX_HEIGHT", 720))
    print(f"[CFG] ANALYTICS_CFG={S.ANALYTICS_CFG}")
    print(f"[CFG] HOMO_YML={S.HOMO_YML}")
    print(f"[CFG] VIDEO_FPS={S.VIDEO_FPS}")
    print(f"[CFG] MUX_WIDTH={S.MUX_WIDTH}  MUX_HEIGHT={S.MUX_HEIGHT}")
    pipeline, nvdsosd, webrtc = build_webrtc_pipeline(args.rtsp_or_file)
    source_pts, target_pts = load_points(str(S.HOMO_YML))
    vt = ViewTransformer(source_pts, target_pts)
    probe = SpeedProbe(vt, roi_source_points=source_pts, cooldown_s=2.5)

    pad = nvdsosd.get_static_pad("sink")
    pad.add_probe(Gst.PadProbeType.BUFFER, probe.osd_sink_pad_buffer_probe, None)

    ws_uri = f"ws://{args.server}:8080/ws?room={args.room}&role=pub"
    session = WebRTCSession(webrtc, ws_uri)
    await session.connect()
    probe.set_publisher(session.send_json_threadsafe)

    loop = GLib.MainLoop()
    pipeline.set_state(Gst.State.PLAYING)
    print("[RUN] Pipeline WebRTC is running...")
    try:
        await asyncio.get_event_loop().run_in_executor(None, loop.run)
    finally:
        pipeline.set_state(Gst.State.NULL)

if __name__ == "__main__":
    Gst.init(None)
    asyncio.run(async_main())
