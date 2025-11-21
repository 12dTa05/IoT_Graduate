# System Performance Optimization Guide

## Overview

Tối ưu hóa toàn diện cho hệ thống multi-edge IoT.

---

## 1. Jetson Performance Tuning

### Set Maximum Performance Mode

```bash
# Check available power modes
sudo nvpmodel -q

# Set to maximum performance (mode 0)
sudo nvpmodel -m 0

# Lock clocks to maximum
sudo jetson_clocks

# Verify
sudo jtop
```

### Fan Control (for cooling)

```bash
# Set fan to 100%
echo 255 > /sys/devices/pwm-fan/target_pwm
```

### Disable GUI (headless mode)

```bash
# Switch to multi-user target (no GUI)
sudo systemctl set-default multi-user.target

# Reboot
sudo reboot

# To re-enable GUI
sudo systemctl set-default graphical.target
```

---

## 2. DeepStream Optimization

### Pipeline Configuration

**Streammux Settings**:
```ini
[streammux]
batch-size=1                    # Low latency
batched-push-timeout=40000      # 40ms timeout
live-source=1                   # RTSP streams
width=1280                      # Resolution
height=720
```

**Inference Settings**:
```ini
[primary-gie]
batch-size=1                    # Real-time
network-mode=2                  # FP16
interval=0                      # Process every frame
```

**Tracker Settings**:
```ini
[tracker]
tracker-width=640               # Lower = faster
tracker-height=384
enable-batch-process=1
```

### Memory Optimization

```bash
# Increase swap (if needed)
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Make permanent
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 3. Network Optimization

### TCP Tuning

```bash
# Increase buffer sizes
sudo sysctl -w net.core.rmem_max=134217728
sudo sysctl -w net.core.wmem_max=134217728
sudo sysctl -w net.ipv4.tcp_rmem='4096 87380 67108864'
sudo sysctl -w net.ipv4.tcp_wmem='4096 65536 67108864'

# Enable TCP window scaling
sudo sysctl -w net.ipv4.tcp_window_scaling=1

# Make permanent
sudo nano /etc/sysctl.conf
# Add above lines
```

### MQTT Optimization

```conf
# mosquitto.conf
max_queued_messages 1000
max_inflight_messages 20
message_size_limit 0
```

### WebSocket Optimization

```python
# In FastAPI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Increase WebSocket buffer
websocket.send_json(data, mode="binary")  # Binary mode faster
```

---

## 4. Database Optimization (if using)

### PostgreSQL Tuning

```sql
-- postgresql.conf
shared_buffers = 256MB
effective_cache_size = 1GB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1
effective_io_concurrency = 200
work_mem = 4MB
min_wal_size = 1GB
max_wal_size = 4GB
```

### Index Optimization

```sql
-- Index on frequently queried columns
CREATE INDEX idx_alerts_timestamp ON alerts(timestamp DESC);
CREATE INDEX idx_alerts_edge_id ON alerts(edge_id);
CREATE INDEX idx_alerts_speed ON alerts(speed) WHERE speed > 60;
```

---

## 5. Python Optimization

### Use uvloop for FastAPI

```python
# main_edge.py
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# 2-4x faster event loop
```

### Optimize JSON Serialization

```python
# Use orjson instead of json
import orjson

# Faster serialization
data = orjson.dumps(metadata)

# Faster deserialization
metadata = orjson.loads(data)
```

### Connection Pooling

```python
# For database connections
from sqlalchemy.pool import QueuePool

engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=20
)
```

---

## 6. ReactJS Optimization

### Code Splitting

```javascript
// Lazy load components
const Dashboard = React.lazy(() => import('./pages/Dashboard'));

// Use Suspense
<Suspense fallback={<Loading />}>
  <Dashboard />
</Suspense>
```

### Memoization

```javascript
// Memoize expensive computations
const memoizedValue = useMemo(() => {
  return expensiveCalculation(data);
}, [data]);

// Memoize callbacks
const memoizedCallback = useCallback(() => {
  doSomething(a, b);
}, [a, b]);
```

### Virtual Scrolling (for alerts list)

```javascript
import { FixedSizeList } from 'react-window';

<FixedSizeList
  height={600}
  itemCount={alerts.length}
  itemSize={50}
>
  {Row}
</FixedSizeList>
```

### Production Build

```bash
# Build with optimizations
npm run build

# Serve with nginx
sudo apt install nginx
sudo cp -r dist/* /var/www/html/
```

---

## 7. Monitoring & Profiling

### System Monitoring

```bash
# Install monitoring tools
sudo apt install htop iotop nethogs

# Monitor GPU
sudo jtop

# Monitor network
sudo nethogs

# Monitor disk I/O
sudo iotop
```

### DeepStream Profiling

```bash
# Enable profiling
export NVDS_ENABLE_PROFILING=1

# Run pipeline
python3 main_edge.py ...

# Check logs for profiling data
```

### Python Profiling

```python
# Use cProfile
import cProfile
import pstats

profiler = cProfile.Profile()
profiler.enable()

# Your code here

profiler.disable()
stats = pstats.Stats(profiler)
stats.sort_stats('cumulative')
stats.print_stats(20)
```

---

## 8. Caching Strategies

### Redis for Metadata Cache

```python
import redis

# Connect to Redis
r = redis.Redis(host='localhost', port=6379, db=0)

# Cache metadata
r.setex(f"metadata:{edge_id}", 60, json.dumps(metadata))

# Get from cache
cached = r.get(f"metadata:{edge_id}")
```

### In-Memory Cache

```python
from functools import lru_cache

@lru_cache(maxsize=128)
def get_edge_config(edge_id):
    # Expensive operation
    return load_config(edge_id)
```

---

## 9. Load Testing

### Test MQTT Throughput

```bash
# Install mosquitto clients
sudo apt install mosquitto-clients

# Publish test messages
for i in {1..1000}; do
  mosquitto_pub -h localhost -t test -m "message $i"
done

# Measure latency
mosquitto_sub -h localhost -t test -v
```

### Test WebSocket

```python
import asyncio
import websockets

async def test_websocket():
    uri = "ws://localhost:8000/ws/metadata"
    async with websockets.connect(uri) as websocket:
        for i in range(1000):
            data = await websocket.recv()
            print(f"Received {len(data)} bytes")

asyncio.run(test_websocket())
```

### Stress Test with Locust

```python
# locustfile.py
from locust import HttpUser, task, between

class EdgeUser(HttpUser):
    wait_time = between(0.1, 0.5)
    
    @task
    def get_metrics(self):
        self.client.get("/api/metrics")
    
    @task(3)
    def get_status(self):
        self.client.get("/api/status")

# Run: locust -f locustfile.py --host=http://localhost:8000
```

---

## 10. Best Practices Checklist

### Performance

- [ ] Jetson set to max performance mode
- [ ] DeepStream using FP16 precision
- [ ] Batch size optimized (1 for latency, 4-8 for throughput)
- [ ] Tracker resolution reduced if needed
- [ ] Network buffers increased
- [ ] uvloop enabled for Python
- [ ] Production build for ReactJS

### Reliability

- [ ] Error handling in all async functions
- [ ] Automatic reconnection for MQTT/WebSocket
- [ ] Fallback to local processing if offload fails
- [ ] Health checks for all services
- [ ] Logging configured properly

### Security

- [ ] MQTT authentication enabled
- [ ] CORS configured properly
- [ ] API rate limiting
- [ ] Input validation
- [ ] HTTPS for production

### Monitoring

- [ ] System metrics collected (CPU, GPU, RAM)
- [ ] Pipeline FPS monitored
- [ ] Network latency tracked
- [ ] Error rates logged
- [ ] Alerts configured

---

## Performance Targets

| Metric | Target | Acceptable | Poor |
|--------|--------|------------|------|
| End-to-end Latency | < 30ms | < 50ms | > 100ms |
| Pipeline FPS | > 30 | > 20 | < 15 |
| GPU Usage | 70-90% | 50-70% | < 50% or > 95% |
| CPU Usage | < 70% | < 85% | > 90% |
| RAM Usage | < 80% | < 90% | > 95% |
| MQTT Latency | < 5ms | < 10ms | > 20ms |
| WebSocket FPS | 25-30 | 20-25 | < 20 |

---

## Troubleshooting Performance Issues

### Low FPS

1. Check GPU usage with `jtop`
2. If GPU < 70%: CPU bottleneck → optimize Python code
3. If GPU > 90%: GPU bottleneck → reduce model size or resolution
4. Check tracker resolution
5. Disable analytics if not needed

### High Latency

1. Check `batched-push-timeout` in streammux
2. Reduce tracker resolution
3. Use FP16 precision
4. Check network latency
5. Optimize WebSocket sending

### Memory Issues

1. Reduce batch size
2. Lower resolution
3. Limit tracker history
4. Clear old alerts from database
5. Add swap if needed

### Network Congestion

1. Reduce JPEG quality for offloading
2. Use ROI-based offloading
3. Adjust offload ratio
4. Check bandwidth with `iftop`
5. Optimize MQTT QoS settings
