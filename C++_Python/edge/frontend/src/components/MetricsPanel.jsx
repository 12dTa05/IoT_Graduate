import React, { useEffect, useState } from 'react'

/**
 * MetricsPanel Component
 * Displays CPU, GPU, RAM, Temperature metrics
 */
export default function MetricsPanel({ apiUrl }) {
    const [metrics, setMetrics] = useState({
        cpu_usage: 0,
        gpu_usage: 0,
        ram_usage: 0,
        temp: 0,
        power: 0
    })

    useEffect(() => {
        const fetchMetrics = async () => {
            try {
                const response = await fetch(`${apiUrl}/api/metrics`)
                const data = await response.json()
                setMetrics(data.metrics || {})
            } catch (error) {
                console.error('Failed to fetch metrics:', error)
            }
        }

        fetchMetrics()
        const interval = setInterval(fetchMetrics, 2000) // Update every 2s

        return () => clearInterval(interval)
    }, [apiUrl])

    const getColor = (value, threshold) => {
        return value > threshold ? '#ff4444' : '#44ff44'
    }

    const MetricItem = ({ label, value, unit, threshold }) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ fontSize: '14px', color: '#aaa' }}>{label}:</span>
            <span style={{
                fontSize: '16px',
                fontWeight: 'bold',
                color: threshold ? getColor(value, threshold) : '#fff'
            }}>
                {value?.toFixed(1) || 0}{unit}
            </span>
        </div>
    )

    return (
        <div style={{
            display: 'flex',
            gap: '20px',
            alignItems: 'center'
        }}>
            <MetricItem label="CPU" value={metrics.cpu_usage} unit="%" threshold={85} />
            <MetricItem label="GPU" value={metrics.gpu_usage} unit="%" threshold={80} />
            <MetricItem label="RAM" value={metrics.ram_usage} unit="%" threshold={90} />
            <MetricItem label="Temp" value={metrics.temp} unit="°C" threshold={75} />
            <MetricItem label="Power" value={metrics.power} unit="W" />
        </div>
    )
}
