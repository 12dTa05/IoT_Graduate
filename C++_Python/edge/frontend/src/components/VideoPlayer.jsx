import React, { useEffect, useRef, useState } from 'react'

/**
 * VideoPlayer Component
 * Displays video stream with bounding box overlays
 * Shows speed and license plate at top-right corner of each bbox
 */
export default function VideoPlayer({ edgeId, apiUrl }) {
    const videoRef = useRef(null)
    const canvasRef = useRef(null)
    const wsRef = useRef(null)
    const [metadata, setMetadata] = useState([])
    const [connected, setConnected] = useState(false)

    useEffect(() => {
        // WebSocket for metadata (bboxes, speed, plates)
        const wsUrl = apiUrl.replace('http', 'ws') + '/ws/metadata'
        wsRef.current = new WebSocket(wsUrl)

        wsRef.current.onopen = () => {
            console.log('WebSocket connected')
            setConnected(true)
        }

        wsRef.current.onmessage = (event) => {
            const data = JSON.parse(event.data)
            setMetadata(data)
        }

        wsRef.current.onerror = (error) => {
            console.error('WebSocket error:', error)
            setConnected(false)
        }

        wsRef.current.onclose = () => {
            console.log('WebSocket closed')
            setConnected(false)
        }

        // TODO: Setup WebRTC for video stream
        // For now, use placeholder video
        setupPlaceholderVideo()

        return () => {
            wsRef.current?.close()
        }
    }, [apiUrl])

    const setupPlaceholderVideo = () => {
        // Placeholder: In production, setup WebRTC connection here
        // For now, just set canvas size
        if (canvasRef.current) {
            canvasRef.current.width = 1280
            canvasRef.current.height = 720
        }
    }

    useEffect(() => {
        // Draw bounding boxes on canvas
        const canvas = canvasRef.current
        if (!canvas) return

        const ctx = canvas.getContext('2d')
        const draw = () => {
            // Clear canvas
            ctx.clearRect(0, 0, canvas.width, canvas.height)

            // Draw each object
            metadata.forEach(obj => {
                const { x, y, width, height, speed, plate, track_id } = obj

                // Determine color based on speed
                const isOverspeed = speed > 60
                const color = isOverspeed ? '#ff3333' : '#33ff33'

                // Draw bounding box
                ctx.strokeStyle = color
                ctx.lineWidth = 3
                ctx.strokeRect(x, y, width, height)

                // Draw label background (top-right corner)
                const labelX = x + width - 120
                const labelY = y - 50
                ctx.fillStyle = 'rgba(0, 0, 0, 0.7)'
                ctx.fillRect(labelX, labelY, 115, 45)

                // Draw speed text
                ctx.fillStyle = color
                ctx.font = 'bold 16px Inter'
                ctx.fillText(`${speed} km/h`, labelX + 5, labelY + 20)

                // Draw license plate
                ctx.fillStyle = '#ffffff'
                ctx.font = '14px Inter'
                ctx.fillText(plate || 'N/A', labelX + 5, labelY + 38)

                // Draw track ID on top-left of bbox
                ctx.fillStyle = color
                ctx.font = 'bold 14px Inter'
                ctx.fillText(`#${track_id}`, x + 5, y + 20)
            })

            requestAnimationFrame(draw)
        }

        draw()
    }, [metadata])

    return (
        <div style={{ width: '100%', height: '100%', position: 'relative' }}>
            {/* Video element (placeholder for now) */}
            <video
                ref={videoRef}
                autoPlay
                playsInline
                muted
                style={{
                    width: '100%',
                    height: '100%',
                    objectFit: 'contain',
                    background: '#000'
                }}
            />

            {/* Canvas overlay for bounding boxes */}
            <canvas
                ref={canvasRef}
                style={{
                    position: 'absolute',
                    top: '50%',
                    left: '50%',
                    transform: 'translate(-50%, -50%)',
                    maxWidth: '100%',
                    maxHeight: '100%',
                    pointerEvents: 'none'
                }}
            />

            {/* Connection status */}
            <div style={{
                position: 'absolute',
                top: 10,
                left: 10,
                background: connected ? 'rgba(0, 255, 0, 0.2)' : 'rgba(255, 0, 0, 0.2)',
                padding: '5px 10px',
                borderRadius: '5px',
                fontSize: '12px',
                border: `1px solid ${connected ? '#0f0' : '#f00'}`
            }}>
                {connected ? '● Connected' : '○ Disconnected'}
            </div>

            {/* Object count */}
            <div style={{
                position: 'absolute',
                top: 10,
                right: 10,
                background: 'rgba(0, 0, 0, 0.7)',
                padding: '5px 10px',
                borderRadius: '5px',
                fontSize: '12px'
            }}>
                Objects: {metadata.length}
            </div>
        </div>
    )
}
