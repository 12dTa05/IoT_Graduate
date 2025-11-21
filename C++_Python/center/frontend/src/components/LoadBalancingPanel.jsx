import React, { useState } from 'react'

/**
 * LoadBalancingPanel Component
 * Shows offload status and manual controls
 */
export default function LoadBalancingPanel({ edges, balancerStatus, centerApiUrl }) {
    const [selectedSource, setSelectedSource] = useState('')
    const [selectedTarget, setSelectedTarget] = useState('')

    const handleManualOffload = async () => {
        if (!selectedSource || !selectedTarget) {
            alert('Please select both source and target edges')
            return
        }

        try {
            const response = await fetch(`${centerApiUrl}/api/balancer/offload?source_edge=${selectedSource}&target_edge=${selectedTarget}`, {
                method: 'POST'
            })
            const data = await response.json()
            console.log('Offload started:', data)
        } catch (error) {
            console.error('Failed to start offload:', error)
        }
    }

    const handleStopOffload = async (edgeId) => {
        try {
            await fetch(`${centerApiUrl}/api/balancer/stop/${edgeId}`, {
                method: 'POST'
            })
            console.log('Offload stopped for', edgeId)
        } catch (error) {
            console.error('Failed to stop offload:', error)
        }
    }

    const activeOffloads = balancerStatus.active_offloads || {}
    const overloadedEdges = balancerStatus.overloaded_edges || []

    return (
        <div style={{ padding: '20px' }}>
            <h2 style={{ fontSize: '20px', marginBottom: '20px', fontWeight: '600' }}>
                Load Balancing
            </h2>

            {/* Status Overview */}
            <div style={{
                background: '#1a1a1a',
                padding: '15px',
                borderRadius: '8px',
                marginBottom: '20px'
            }}>
                <h3 style={{ fontSize: '14px', marginBottom: '10px', color: '#aaa' }}>Status</h3>
                <div style={{ fontSize: '13px' }}>
                    <div>Active Offloads: {Object.keys(activeOffloads).length}</div>
                    <div style={{ color: '#ff4444' }}>
                        Overloaded Edges: {overloadedEdges.length}
                    </div>
                </div>
            </div>

            {/* Active Offloads */}
            {Object.keys(activeOffloads).length > 0 && (
                <div style={{
                    background: '#1a1a1a',
                    padding: '15px',
                    borderRadius: '8px',
                    marginBottom: '20px'
                }}>
                    <h3 style={{ fontSize: '14px', marginBottom: '10px', color: '#aaa' }}>
                        Active Offloads
                    </h3>
                    {Object.entries(activeOffloads).map(([source, target]) => (
                        <div key={source} style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            padding: '8px',
                            background: '#222',
                            borderRadius: '5px',
                            marginBottom: '5px',
                            fontSize: '12px'
                        }}>
                            <span>{source} → {target}</span>
                            <button
                                onClick={() => handleStopOffload(source)}
                                style={{
                                    background: '#ff4444',
                                    border: 'none',
                                    padding: '4px 8px',
                                    borderRadius: '3px',
                                    color: '#fff',
                                    cursor: 'pointer',
                                    fontSize: '11px'
                                }}
                            >
                                Stop
                            </button>
                        </div>
                    ))}
                </div>
            )}

            {/* Manual Control */}
            <div style={{
                background: '#1a1a1a',
                padding: '15px',
                borderRadius: '8px'
            }}>
                <h3 style={{ fontSize: '14px', marginBottom: '10px', color: '#aaa' }}>
                    Manual Offload
                </h3>

                <div style={{ marginBottom: '10px' }}>
                    <label style={{ fontSize: '12px', color: '#aaa', display: 'block', marginBottom: '5px' }}>
                        Source Edge (Overloaded)
                    </label>
                    <select
                        value={selectedSource}
                        onChange={(e) => setSelectedSource(e.target.value)}
                        style={{
                            width: '100%',
                            padding: '8px',
                            background: '#222',
                            border: '1px solid #333',
                            borderRadius: '5px',
                            color: '#fff',
                            fontSize: '13px'
                        }}
                    >
                        <option value="">Select source...</option>
                        {edges.filter(e => e.status === 'online').map(edge => (
                            <option key={edge.id} value={edge.id}>{edge.id}</option>
                        ))}
                    </select>
                </div>

                <div style={{ marginBottom: '15px' }}>
                    <label style={{ fontSize: '12px', color: '#aaa', display: 'block', marginBottom: '5px' }}>
                        Target Edge (Idle)
                    </label>
                    <select
                        value={selectedTarget}
                        onChange={(e) => setSelectedTarget(e.target.value)}
                        style={{
                            width: '100%',
                            padding: '8px',
                            background: '#222',
                            border: '1px solid #333',
                            borderRadius: '5px',
                            color: '#fff',
                            fontSize: '13px'
                        }}
                    >
                        <option value="">Select target...</option>
                        {edges.filter(e => e.status === 'online' && e.id !== selectedSource).map(edge => (
                            <option key={edge.id} value={edge.id}>{edge.id}</option>
                        ))}
                    </select>
                </div>

                <button
                    onClick={handleManualOffload}
                    style={{
                        width: '100%',
                        padding: '10px',
                        background: '#0066ff',
                        border: 'none',
                        borderRadius: '5px',
                        color: '#fff',
                        fontSize: '14px',
                        fontWeight: '600',
                        cursor: 'pointer'
                    }}
                >
                    Start Offload
                </button>
            </div>

            {/* Overloaded Edges Warning */}
            {overloadedEdges.length > 0 && (
                <div style={{
                    marginTop: '20px',
                    padding: '10px',
                    background: 'rgba(255, 68, 68, 0.1)',
                    border: '1px solid #ff4444',
                    borderRadius: '5px',
                    fontSize: '12px'
                }}>
                    <strong>⚠ Warning:</strong> {overloadedEdges.join(', ')} overloaded
                </div>
            )}
        </div>
    )
}
