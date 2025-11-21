"""
WebRTC Signaling Server
Handles SDP offer/answer and ICE candidate exchange
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Set
import json
import logging
import asyncio

logger = logging.getLogger(__name__)


class WebRTCSignalingServer:
    """
    WebRTC signaling server for SDP and ICE exchange
    Connects edge pipeline to dashboard clients
    """
    
    def __init__(self):
        # Connected clients (WebSocket connections)
        self.clients: Set[WebSocket] = set()
        
        # Pending offers from edge
        self.pending_offers: Dict[str, str] = {}
        
        # ICE candidates buffer
        self.ice_candidates: Dict[str, list] = {}
        
        # Callback for sending to C++ pipeline
        self.pipeline_callback = None
    
    async def handle_client(self, websocket: WebSocket, client_id: str):
        """
        Handle WebSocket connection from dashboard client
        
        Args:
            websocket: WebSocket connection
            client_id: Unique client identifier
        """
        await websocket.accept()
        self.clients.add(websocket)
        logger.info(f"Client {client_id} connected (total: {len(self.clients)})")
        
        try:
            # Send pending offer if available
            if client_id in self.pending_offers:
                await websocket.send_json({
                    "type": "offer",
                    "sdp": self.pending_offers[client_id]
                })
                
                # Send buffered ICE candidates
                if client_id in self.ice_candidates:
                    for candidate in self.ice_candidates[client_id]:
                        await websocket.send_json(candidate)
                    self.ice_candidates[client_id].clear()
            
            # Handle messages from client
            while True:
                message = await websocket.receive_json()
                await self.handle_message(websocket, client_id, message)
        
        except WebSocketDisconnect:
            self.clients.remove(websocket)
            logger.info(f"Client {client_id} disconnected")
        except Exception as e:
            logger.error(f"Error handling client {client_id}: {e}")
            if websocket in self.clients:
                self.clients.remove(websocket)
    
    async def handle_message(self, websocket: WebSocket, client_id: str, message: dict):
        """
        Handle signaling message from client
        
        Args:
            websocket: Client WebSocket
            client_id: Client ID
            message: Signaling message
        """
        msg_type = message.get("type")
        
        if msg_type == "answer":
            # SDP answer from client
            sdp = message.get("sdp")
            logger.info(f"Received answer from {client_id}")
            
            # Send to C++ pipeline
            if self.pipeline_callback:
                self.pipeline_callback("set_remote_description", sdp)
        
        elif msg_type == "ice-candidate":
            # ICE candidate from client
            candidate = message.get("candidate")
            sdp_mline_index = message.get("sdpMLineIndex", 0)
            
            logger.info(f"Received ICE candidate from {client_id}")
            
            # Send to C++ pipeline
            if self.pipeline_callback:
                self.pipeline_callback("add_ice_candidate", {
                    "candidate": candidate,
                    "sdpMLineIndex": sdp_mline_index
                })
    
    async def send_offer(self, client_id: str, sdp: str):
        """
        Send SDP offer to client
        Called by C++ pipeline
        
        Args:
            client_id: Target client ID
            sdp: SDP offer string
        """
        self.pending_offers[client_id] = sdp
        
        # Send to connected clients
        for client in self.clients:
            try:
                await client.send_json({
                    "type": "offer",
                    "sdp": sdp
                })
                logger.info(f"Sent offer to client")
            except Exception as e:
                logger.error(f"Failed to send offer: {e}")
    
    async def send_ice_candidate(self, client_id: str, candidate: str, sdp_mline_index: int):
        """
        Send ICE candidate to client
        Called by C++ pipeline
        
        Args:
            client_id: Target client ID
            candidate: ICE candidate string
            sdp_mline_index: SDP m-line index
        """
        message = {
            "type": "ice-candidate",
            "candidate": candidate,
            "sdpMLineIndex": sdp_mline_index
        }
        
        # Buffer if no clients connected yet
        if not self.clients:
            if client_id not in self.ice_candidates:
                self.ice_candidates[client_id] = []
            self.ice_candidates[client_id].append(message)
            return
        
        # Send to connected clients
        for client in self.clients:
            try:
                await client.send_json(message)
            except Exception as e:
                logger.error(f"Failed to send ICE candidate: {e}")
    
    def set_pipeline_callback(self, callback):
        """
        Set callback for sending messages to C++ pipeline
        
        Args:
            callback: Function(action, data)
        """
        self.pipeline_callback = callback


# Global signaling server instance
signaling_server = WebRTCSignalingServer()


def get_signaling_server() -> WebRTCSignalingServer:
    """Get global signaling server instance"""
    return signaling_server
