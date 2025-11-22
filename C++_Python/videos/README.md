# Videos Directory

Place your test videos here for standalone edge testing.

## Supported formats:
- MP4 (H.264/H.265)
- AVI
- MKV
- MOV

## Sample videos:

You can download sample traffic videos from:
- https://www.pexels.com/search/videos/traffic/
- https://pixabay.com/videos/search/traffic/

## Usage:

```bash
# Place video here
cp /path/to/your/video.mp4 ./sample.mp4

# Run with Docker
docker run --runtime nvidia --rm -it \
  -p 8000:8000 \
  -v $(pwd):/app/videos \
  edge-node-standalone \
  python3 main_edge_standalone.py --source file:///app/videos/sample.mp4
```
