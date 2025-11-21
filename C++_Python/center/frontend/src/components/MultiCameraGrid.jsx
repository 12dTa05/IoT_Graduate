import React from 'react'
import EdgeMetricsCard from './EdgeMetricsCard'

/**
 * MultiCameraGrid Component
 * Displays 4 camera feeds in 2x2 grid
 */
export default function MultiCameraGrid({ edges }) {
    // Ensure we always show 4 slots (even if some edges are offline)
    const edgeSlots = [...edges]
    while (edgeSlots.length < 4) {
        edgeSlots.push({ id: `empty_${edgeSlots.length}`, status: 'offline', metrics: {} })
    }

    return (
        <div style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gridTemplateRows: '1fr 1fr',
            gap: '15px',
            padding: '15px',
            height: '100%'
        }}>
            {edgeSlots.slice(0, 4).map((edge, index) => (
                <EdgeCameraView key={edge.id} edge={edge} index={index} />
            ))}
        </div>
    )
}

function EdgeCameraView({ edge, index }) {
    const isOnline = edge.status === 'online'

    return (
        <div style={{
            border: '2px solid #333',
            borderRadius: '8px',
            overflow: 'hidden',
            background: '#1a1a1a',
            display: 'flex',
            flexDirection: 'column'
        }}>
            {/* Header */}
            <div style={{
                background: '#222',
                padding: '10px 15px',
                borderBottom: '1px solid #333',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center'
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <h3 style={{ fontSize: '16px', fontWeight: '600' }}>
                        {edge.id}
                    </h3>
                    <div style={{
                        width: '8px',
                        height: '8px',
                        borderRadius: '50%',
                        background: isOnline ? '#0f0' : '#f00'
                    }} />
                </div>
                <EdgeMetricsCard metrics={edge.metrics || {}} compact />
            </div>

            {/* Video Area */}
            <div style={{
                flex: 1,
                background: '#000',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                position: 'relative',
                minHeight: '300px'
            }}>
                {isOnline ? (
                    <div style={{ color: '#666', fontSize: '14px' }}>
                        {/* TODO: Embed VideoPlayer component from edge */}
                        Camera {index + 1} - Stream Active
                    </div>
                ) : (
                    <div style={{ color: '#666', fontSize: '14px' }}>
                        Camera {index + 1} - Offline
                    </div>
                )}
            </div>
        </div>
    )
}
