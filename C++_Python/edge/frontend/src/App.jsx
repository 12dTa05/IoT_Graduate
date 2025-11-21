import React from 'react'
import VideoPlayer from './components/VideoPlayer'
import MetricsPanel from './components/MetricsPanel'

function App() {
    const edgeId = 'edge_1' // TODO: Get from URL params or config
    const apiUrl = 'http://localhost:8000'

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
                <h1 style={{ fontSize: '24px', fontWeight: '600' }}>Edge Node: {edgeId}</h1>
                <MetricsPanel apiUrl={apiUrl} />
            </div>

            {/* Video Player */}
            <div style={{ flex: 1, position: 'relative', background: '#000' }}>
                <VideoPlayer edgeId={edgeId} apiUrl={apiUrl} />
            </div>
        </div>
    )
}

export default App
