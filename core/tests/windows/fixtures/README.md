# W5 Review fixture

`w5-review.mp4` is synthetic test media generated with FFmpeg only.

- video: 1 second, 320x180, 25 fps, H.264, solid black
- audio: 1 kHz synthetic sine wave, AAC, 48 kHz mono
- no third-party image, video, speech, music, or other media content

Generation command:

```sh
ffmpeg -f lavfi -i color=c=black:s=320x180:r=25:d=1 \
  -f lavfi -i sine=frequency=1000:sample_rate=48000:duration=1 \
  -c:v libx264 -pix_fmt yuv420p -profile:v baseline -level 3.0 \
  -c:a aac -b:a 64k -shortest -movflags +faststart \
  -metadata title="Roughcut synthetic W5 review test fixture" \
  -metadata comment="Synthetic media generated with FFmpeg; no third-party media content." \
  w5-review.mp4
```

SHA-256 of the committed fixture:

`18788615c1549c01bce833ab61ef07d4c1400a8daa5871356bd6d9f76d7ea3d0`
