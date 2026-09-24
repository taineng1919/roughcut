# W5 Review fixture

`w5-review.mp4` is synthetic test media generated with FFmpeg only.

- video: 1 second, 320x180, 25 fps, H.264, solid black
- audio: synthetic silence, AAC, 48 kHz mono
- no third-party image, video, speech, music, or other media content

Generation command:

```sh
ffmpeg -f lavfi -i color=c=black:s=320x180:r=25:d=1 \
  -f lavfi -i anullsrc=r=48000:cl=mono:d=1 \
  -c:v libx264 -preset veryslow -tune stillimage -pix_fmt yuv420p \
  -profile:v baseline -level 3.0 -c:a aac -b:a 8k -shortest \
  -movflags +faststart \
  -metadata title="Roughcut synthetic W5 review test fixture" \
  -metadata comment="Synthetic media generated with FFmpeg lavfi only; no third-party media content." \
  w5-review.mp4
```

SHA-256 of the committed fixture:

`2421f5637b124b58609ca7534d1a3d3527ac737a4d85397ac696dfdb89dadc28`
