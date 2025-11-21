import React, { useEffect, useRef, useState } from 'react';
import './VideoPlayer.css';

const VideoPlayer = () => {
    const videoRef = useRef(null);
    const canvasRef = useRef(null);
    const [objects, setObjects] = useState([]);
    const [isConnected, setIsConnected] = useState(false);

    // WebRTC connection
    const peerConnectionRef = useRef(null);
    const signalingWsRef = useRef(null);

    // Metadata WebSocket
    const metadataWsRef = useRef(null);

    useEffect(() => {
        setupWebRTC();
        setupMetadataWebSocket();

        return () => {
            cleanup();
        };
    }, []);

    const setupWebRTC = async () => {
        // Create peer connection
        const configuration = {
            iceServers: [
                { urls: 'stun:stun.l.google.com:19302' },
                { urls: 'stun:stun1.l.google.com:19302' }
            ]
        };

        const pc = new RTCPeerConnection(configuration);
        peerConnectionRef.current = pc;

        // Handle incoming track
        pc.ontrack = (event) => {
            console.log('Received remote track');
            if (videoRef.current) {
                videoRef.current.srcObject = event.streams[0];
            }
        };

        // Handle ICE candidates
        pc.onicecandidate = (event) => {
            if (event.candidate && signalingWsRef.current) {
                signalingWsRef.current.send(JSON.stringify({
                    type: 'ice-candidate',
                    candidate: event.candidate.candidate,
                    sdpMLineIndex: event.candidate.sdpMLineIndex
                }));
            }
        };

        // Connection state
        pc.onconnectionstatechange = () => {
            console.log('Connection state:', pc.connectionState);
            setIsConnected(pc.connectionState === 'connected');
        };

        // Connect to signaling server
        const signalingWs = new WebSocket('ws://localhost:8000/ws/signaling');
        signalingWsRef.current = signalingWs;

        signalingWs.onopen = () => {
            console.log('Signaling WebSocket connected');
        };

        signalingWs.onmessage = async (event) => {
            const message = JSON.parse(event.data);

            if (message.type === 'offer') {
                console.log('Received offer');

                // Set remote description
                await pc.setRemoteDescription({
                    type: 'offer',
                    sdp: message.sdp
                });

                // Create answer
                const answer = await pc.createAnswer();
                await pc.setLocalDescription(answer);

                // Send answer
                signalingWs.send(JSON.stringify({
                    type: 'answer',
                    sdp: answer.sdp
                }));
            } else if (message.type === 'ice-candidate') {
                console.log('Received ICE candidate');

                await pc.addIceCandidate({
                    candidate: message.candidate,
                    sdpMLineIndex: message.sdpMLineIndex
                });
            }
        };

        signalingWs.onerror = (error) => {
            console.error('Signaling WebSocket error:', error);
        };

        signalingWs.onclose = () => {
            console.log('Signaling WebSocket closed');
            setIsConnected(false);
        };
    };

    const setupMetadataWebSocket = () => {
        const ws = new WebSocket('ws://localhost:8000/ws/metadata');
        metadataWsRef.current = ws;

        ws.onopen = () => {
            console.log('Metadata WebSocket connected');
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            setObjects(data);
        };

        ws.onerror = (error) => {
            console.error('Metadata WebSocket error:', error);
        };

        ws.onclose = () => {
            console.log('Metadata WebSocket closed');
            // Reconnect after 3 seconds
            setTimeout(setupMetadataWebSocket, 3000);
        };
    };

    const cleanup = () => {
        if (peerConnectionRef.current) {
            peerConnectionRef.current.close();
        }
        if (signalingWsRef.current) {
            signalingWsRef.current.close();
        }
        if (metadataWsRef.current) {
            metadataWsRef.current.close();
        }
    };

    // Draw bounding boxes on canvas
    useEffect(() => {
        const canvas = canvasRef.current;
        const video = videoRef.current;

        if (!canvas || !video) return;

        const ctx = canvas.getContext('2d');

        const drawBoxes = () => {
            // Match canvas size to video
            canvas.width = video.videoWidth || 1280;
            canvas.height = video.videoHeight || 720;

            // Clear canvas
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Draw each object
            objects.forEach(obj => {
                const { x, y, width, height, speed, plate, track_id } = obj;

                // Color based on speed
                const isOverspeed = speed > 60;
                const color = isOverspeed ? '#ff4444' : '#44ff44';

                // Draw bounding box
                ctx.strokeStyle = color;
                ctx.lineWidth = 3;
                ctx.strokeRect(x, y, width, height);

                // Draw track ID (top-left)
                ctx.fillStyle = color;
                ctx.font = 'bold 16px Arial';
                ctx.fillText(`ID: ${track_id}`, x + 5, y + 20);

                // Draw speed and plate (top-right)
                if (speed || plate) {
                    const text = `${speed ? speed.toFixed(0) + ' km/h' : ''} ${plate || ''}`.trim();
                    const textWidth = ctx.measureText(text).width;

                    // Background
                    ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
                    ctx.fillRect(x + width - textWidth - 10, y, textWidth + 10, 25);

                    // Text
                    ctx.fillStyle = color;
                    ctx.fillText(text, x + width - textWidth - 5, y + 18);
                }
            });

            requestAnimationFrame(drawBoxes);
        };

        drawBoxes();
    }, [objects]);

    return (
        <div className="video-player">
            <div className="video-container">
                <video
                    ref={videoRef}
                    autoPlay
                    playsInline
                    muted
                    className="video-stream"
                />
                <canvas
                    ref={canvasRef}
                    className="overlay-canvas"
                />
                <div className={`connection-status ${isConnected ? 'connected' : 'disconnected'}`}>
                    {isConnected ? '● Live' : '○ Connecting...'}
                </div>
            </div>
            <div className="stats">
                <span>Objects: {objects.length}</span>
                <span>FPS: {objects.length > 0 ? '30' : '0'}</span>
            </div>
        </div>
    );
};

export default VideoPlayer;
