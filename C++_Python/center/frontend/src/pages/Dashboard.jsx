import React, { useEffect, useState } from 'react'
import MultiCameraGrid from '../components/MultiCameraGrid'
import LoadBalancingPanel from '../components/LoadBalancingPanel'

/**
 * Main Dashboard Page
 * Shows 4 camera feeds in 2x2 grid + load balancing controls
 */
export default function Dashboard() {
    const centerApiUrl = 'http://localhost:8080'
    const [edges, setEdges] = useState([])
    const [balancerStatus, setBalancerStatus] = useState({})

    useEffect(() => {
        // Fetch edges from center API
        const fetchEdges = async () => {
            try {
                const response = await fetch(`${centerApiUrl}/api/edges`)
                const data = await response.json()
                setEdges(data.edges || [])
            } catch (error) {
                console.error('Failed to fetch edges:', error)
            }
        }

        // Fetch balancer status
        const fetchBalancerStatus = async () => {
            try {
                const response = await fetch(`${centerApiUrl}/api/balancer/status`)
                const data = await response.json()
                setBalancerStatus(data)
            } catch (error) {
                console.error('Failed to fetch balancer status:', error)
            }
        }

        fetchEdges()
        fetchBalancerStatus()

        const interval = setInterval(() => {
            fetchEdges()
            fetchBalancerStatus()
        }, 2000)

        return () => clearInterval(interval)
    }, [])

    return (
        <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column' }}>
            {/* Header */}
            <div style={{
                background: '#1a1a1a',
                padding: '15px 30px',
                borderBottom: '2px solid #333',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center'
            }}>
                <h1 style={{ fontSize: '28px', fontWeight: '600' }}>
                    Multi-Edge Monitoring Center
                </h1>
                <div style={{ fontSize: '14px', color: '#aaa' }}>
                    Active Edges: {edges.filter(e => e.status === 'online').length} / {edges.length}
                </div>
            </div>

            {/* Main Content */}
            <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
                {/* Camera Grid */}
                <div style={{ flex: 1, overflow: 'auto' }}>
                    <MultiCameraGrid edges={edges} />
                </div>

                {/* Side Panel - Load Balancing */}
                <div style={{
                    width: '350px',
                    background: '#111',
                    borderLeft: '2px solid #333',
                    overflow: 'auto'
                }}>
                    <LoadBalancingPanel
                        edges={edges}
                        balancerStatus={balancerStatus}
                        centerApiUrl={centerApiUrl}
                    />
                </div>
            </div>
        </div>
    )
}
