import React from 'react'

/**
 * EdgeMetricsCard Component
 * Shows CPU, GPU, FPS, Temp for an edge
 */
export default function EdgeMetricsCard({ metrics, compact = false }) {
    const getColor = (value, threshold) => {
        return value > threshold ? '#ff4444' : '#44ff44'
    }

    const MetricItem = ({ label, value, unit, threshold }) => (
        <div style={{ fontSize: compact ? '11px' : '13px' }}>
            <span style={{ color: '#aaa' }}>{label}: </span>
            <span style={{
                fontWeight: 'bold',
                color: threshold ? getColor(value, threshold) : '#fff'
            }}>
                {value?.toFixed(1) || 0}{unit}
            </span>
        </div>
    )

    if (compact) {
        return (
            <div style={{ display: 'flex', gap: '10px' }}>
                <MetricItem label="CPU" value={metrics.cpu_usage} unit="%" threshold={85} />
                <MetricItem label="GPU" value={metrics.gpu_usage} unit="%" threshold={80} />
            </div>
        )
    }

    return (
        <div style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: '8px',
            padding: '10px',
            background: '#1a1a1a',
            borderRadius: '5px'
        }}>
            <MetricItem label="CPU" value={metrics.cpu_usage} unit="%" threshold={85} />
            <MetricItem label="GPU" value={metrics.gpu_usage} unit="%" threshold={80} />
            <MetricItem label="RAM" value={metrics.ram_usage} unit="%" threshold={90} />
            <MetricItem label="Temp" value={metrics.temp} unit="°C" threshold={75} />
        </div>
    )
}
