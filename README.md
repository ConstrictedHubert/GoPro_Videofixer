# GoPro Videofixer

Fixes the two GoPro-on-Mac annoyances that come from importing via Image Capture:

- imported file dates reset to 1970
- long recordings get split into chaptered files (`GH010369.MP4`, `GH020369.MP4`, ...)

For every chaptered clip found in a source folder, the script:

1. Reads the GPS telemetry embedded in the first chapter (position + true UTC time of
   the first GPS fix, i.e. roughly when recording started).
2. Resolves that position to a place name -- a known racetrack if the fix is close to
   one, otherwise the nearest city -- and a timezone, **fully offline** (no API key, no
   internet needed at run time), and converts the timestamp to local time.
3. Concatenates all chapters of the clip in order and transcodes them into a single
   H.264 MP4 (source resolution/framerate kept, not forced to 1080p).
4. Names the result `<City>_<YYYY-MM-DD>_<HH-MM>.mp4`, e.g. `Munich_2026-08-15_14-34.mp4`.

If a clip has no GPS fix (GPS was off), the city becomes `UnknownLocation` and the
timestamp falls back to the camera's own clock (no timezone correction possible).

## Setup (one-time)

```bash
brew install ffmpeg exiftool
cd /Users/axel/Github_Projects/GoPro_Videofixer
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

This has already been done in this project folder.

## Usage

Import via Image Capture as usual, then point the script at the destination folder:

```bash
./gopro-videofixer ~/Pictures/GoPro-Import
```

Or point it directly at a mounted camera's `DCIM/100GOPRO` folder if you mount it as a
drive instead of using Image Capture.

Useful flags:

```bash
./gopro-videofixer ~/Pictures/GoPro-Import --list-only   # just show groups + filenames, no encoding
./gopro-videofixer ~/Pictures/GoPro-Import --dry-run      # print the ffmpeg commands instead of running them
./gopro-videofixer ~/Pictures/GoPro-Import -o ~/Movies/GoPro   # custom output folder (default: <source>/Exported)
./gopro-videofixer ~/Pictures/GoPro-Import --quality 75   # higher videotoolbox quality (1-100, default 65)
./gopro-videofixer ~/Pictures/GoPro-Import --encoder libx264 --crf 18 --preset slow  # software encode instead
./gopro-videofixer ~/Pictures/GoPro-Import --track-radius-km 0   # never use racetrack names, always nearest city
./gopro-videofixer ~/Pictures/GoPro-Import --format quicktime    # standardize to QuickTime's 1080p export preset
./gopro-videofixer ~/Pictures/GoPro-Import --format quicktime --bitrate 20M  # ...at a custom bitrate
```

### Output format

Two switchable output formats, via `--format`:

- **`original`** (default) — keeps the source resolution/framerate, quality-based encode
  (`--quality`/`--crf` depending on encoder). Smaller files for the same visual quality,
  but file size and resolution vary per clip.
- **`quicktime`** — matches QuickTime Player's own 1080p export preset: scales down to
  fit 1920x1080 if the source is larger (never upscales smaller sources, keeps aspect
  ratio), encodes at a fixed `--bitrate` (default `16M`, matching QuickTime's own
  output) instead of a quality scale, and standardizes audio to 48kHz stereo AAC. Use
  this when you want consistent, predictable output specs across clips regardless of
  source resolution.

### Encoder

By default this uses Apple's hardware H.264 encoder (VideoToolbox) via `-c:v
h264_videotoolbox`, since software `libx264` on a fanless MacBook Air chokes on GoPro
footage (single-digit fps, 600%+ CPU, thermal throttling on long clips). Hardware
encoding is dramatically faster and uses a fraction of the CPU, at the cost of somewhat
larger files for the same visual quality compared to software x264. Quality is
controlled with `--quality` (1-100, higher = better/larger; default 65) rather than
CRF. Pass `--encoder libx264` to fall back to software encoding with the usual
`--crf`/`--preset` controls if you want the best possible compression efficiency and
don't mind the wait.

### Optional: read straight from the camera

```bash
brew install libgphoto2 gphoto2
./gopro-videofixer --from-camera
```

This uses `gphoto2` (PTP) to pull files into a temp folder before processing. GoPro PTP
support varies by camera model and can be unreliable — if it doesn't detect your camera,
just keep using Image Capture and pass the import folder as the source instead.

## Notes / limitations

- Clip grouping matches GoPro's chaptering scheme: `G<letter><chapter><clip-id>.MP4`
  (e.g. `GH01`/`GH02`/`GH03`... or `GX01`/`GX02`... for HEVC-encoded clips), grouped by
  the 4-digit clip id and ordered by chapter.
- That clip id is just a counter that increments per recording -- it wraps after 9999
  and resets if the SD card is reformatted, so it's not a stable unique identifier. If
  a source folder ends up with footage from different sessions/cards that happen to
  reuse the same clip id, the script cross-checks that consecutive "chapters" are
  actually back-to-back (chapter N's camera-clock end time lines up with chapter N+1's
  start, within 60s) before merging them, and splits them into separate clips instead
  of silently concatenating unrelated footage if they don't line up.
- The GPS timestamp comes from the telemetry stream (true satellite UTC), not the
  file's `CreateDate` metadata atom, since GoPros are known to sometimes write local
  time into that field. Timezone conversion is derived from the GPS coordinates
  themselves (`timezonefinder`), so the printed local time should be correct even if
  your Mac is set to a different timezone.
- City names come from `reverse_geocoder`'s offline worldwide-cities database — this
  gives the nearest known city, not a precise reverse-geocoded address.
- Racetrack names come from a bundled list (`data/racetracks.json`, ~1160 circuits)
  built from Wikidata. If the GPS fix is within `--track-radius-km` (default 5km) of a
  known track, the track name is used instead of the nearest city. Set
  `--track-radius-km 0` to always use the nearest city. Wikidata's coordinate for a
  track is usually a single reference point, not the full circuit outline, so very
  large circuits may need a bigger radius to be recognized everywhere on track. Run
  `python3 data/build_racetracks.py` to refresh the list from Wikidata (needs
  internet); a couple of known-bad upstream coordinates are corrected in that script's
  `COORD_FIXES` table (found by spot-checking after a track resolved to the wrong
  place — Wikidata is crowd-sourced, so more of these may turn up over time).
- Audio is only included in the output if every chapter has an audio track; otherwise
  the output is video-only.
- Safe to re-run/interrupt: before encoding a clip, the script checks whether its
  output path already exists and compares its duration against the source chapters'
  total duration. A match means it's already fully encoded, so that clip is skipped;
  a mismatch (shorter, or unreadable) means it's a truncated leftover from an earlier
  crash/interruption, so it's re-encoded in place. Only an existing file that's
  *longer* than the clip could ever produce is left alone as an unrelated file, with
  a `-2`, `-3`, ... suffix appended instead (e.g. two different clips starting in the
  same minute).
