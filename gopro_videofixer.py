#!/usr/bin/env python3
"""
gopro_videofixer.py

Fixes two GoPro-on-Mac annoyances after importing via Image Capture:
  a) file dates reset to 1970
  b) long recordings are split into chaptered files (GH010369.MP4, GH020369.MP4, ...)

For each chaptered clip group found in a source folder, this script:
  1. Reads the GPS telemetry embedded in the GoPro's first chapter (position + true
     UTC timestamp of the first GPS fix, i.e. roughly when recording started).
  2. Resolves that position to a place name (a known racetrack if the fix is close to
     one, otherwise the nearest city) and local timezone, fully offline (reverse_geocoder
     + timezonefinder + a bundled Wikidata racetrack list), and converts the UTC
     timestamp to local time.
  3. Concatenates all chapters of the clip (in order) and transcodes them to a single
     H.264 MP4, keeping the source resolution/framerate.
  4. Names the output "<City>_<YYYY-MM-DD>_<HH-MM>.mp4".

If a clip has no GPS fix (GPS was off during recording), the city becomes
"UnknownLocation" and the timestamp falls back to the file's embedded (camera clock)
creation date -- which has no reliable timezone info, so it is used as-is.

Requires: ffmpeg, ffprobe, exiftool (brew install ffmpeg exiftool)
and the Python packages in requirements.txt (reverse_geocoder, timezonefinder).
"""

import argparse
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

import reverse_geocoder as rg
from timezonefinder import TimezoneFinder

CLIP_RE = re.compile(r"^G[A-Z](\d{2})(\d{4})\.mp4$", re.IGNORECASE)

_tf = TimezoneFinder()


# --------------------------------------------------------------------------
# Setup / sanity checks
# --------------------------------------------------------------------------

def check_tool(name):
    if shutil.which(name) is None:
        sys.exit(f"error: required tool '{name}' not found in PATH. Install with: brew install {name}")


# --------------------------------------------------------------------------
# Finding and grouping chaptered clips
# --------------------------------------------------------------------------

def find_clip_groups(source_dir: Path):
    """Group GoPro files like GH010369.MP4, GH020369.MP4 by their 4-digit clip id,
    ordered by chapter number. Returns a list of (clip_id, [chapter_paths...])
    sorted by clip id, ascending."""
    groups = {}
    for p in sorted(source_dir.iterdir()):
        if not p.is_file():
            continue
        m = CLIP_RE.match(p.name)
        if not m:
            continue
        chapter, clip_id = int(m.group(1)), m.group(2)
        groups.setdefault(clip_id, []).append((chapter, p))

    result = []
    for clip_id, chapters in groups.items():
        chapters.sort(key=lambda t: t[0])
        nums = [c for c, _ in chapters]
        if nums != list(range(1, len(nums) + 1)):
            print(f"warning: clip {clip_id} has non-contiguous chapters {nums} -- "
                  f"using them in the order found, but a file may be missing", file=sys.stderr)
        result.append((clip_id, [p for _, p in chapters]))

    result.sort(key=lambda t: int(t[0]))
    return result


CHAPTER_GAP_TOLERANCE = 60  # seconds


def probe_duration(path: Path):
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nk=1:nw=1", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def chapter_start_time(path: Path):
    """Best-effort camera-clock start time for continuity checks only (not used for
    naming/GPS -- just to sanity-check that consecutive chapters are back-to-back)."""
    try:
        info = get_exiftool_json(path, ["-MediaCreateDate", "-CreateDate"])
        for key in ("MediaCreateDate", "CreateDate"):
            if info.get(key):
                dt = parse_camera_datetime(str(info[key]))
                if dt:
                    return dt
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return None


def split_on_continuity(clip_id, chapters):
    """GoPro's clip-id counter is just a number that counts up across the camera's
    (or SD card's) whole recording history -- it wraps after 9999 and resets on a
    card reformat. If footage from different sessions/cards ends up in the same
    source folder, two unrelated recordings can coincidentally share a clip id.
    Verify chapters are actually back-to-back (chapter N ends right where chapter
    N+1 starts) before trusting the filename grouping, instead of silently
    concatenating unrelated footage."""
    if len(chapters) == 1:
        return [chapters]

    starts = [chapter_start_time(p) for p in chapters]
    durations = [probe_duration(p) for p in chapters]

    groups = [[chapters[0]]]
    for i in range(1, len(chapters)):
        prev_start, prev_dur, cur_start = starts[i - 1], durations[i - 1], starts[i]
        continuous = True
        if prev_start and prev_dur is not None and cur_start:
            expected = prev_start + timedelta(seconds=prev_dur)
            gap = abs((cur_start - expected).total_seconds())
            continuous = gap <= CHAPTER_GAP_TOLERANCE
        if continuous:
            groups[-1].append(chapters[i])
        else:
            print(f"  warning: clip {clip_id} chapter {chapters[i].name} doesn't look "
                  f"continuous with {chapters[i - 1].name} (GoPro's clip-id counter can "
                  f"be reused across separate recordings) -- treating it as a separate "
                  f"clip instead of merging", file=sys.stderr)
            groups.append([chapters[i]])
    return groups


# --------------------------------------------------------------------------
# exiftool metadata extraction
# --------------------------------------------------------------------------

def get_exiftool_json(path: Path, extra_args):
    cmd = ["exiftool", "-api", "LargeFileSupport=1", "-json"] + extra_args + [str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(proc.stdout)
    return data[0] if data else {}


def extract_gps_samples(path: Path):
    """Pull every embedded GPS fix (lat, lon, UTC timestamp) from the GoPro's
    telemetry track, in chronological order. Returns a list of
    (group_label, lat, lon, gps_datetime_str)."""
    cmd = [
        "exiftool", "-ee", "-G3", "-api", "LargeFileSupport=1", "-json",
        "-c", "%+.6f", "-GPSLatitude", "-GPSLongitude", "-GPSDateTime", "-GPSMeasureMode",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]

    samples = []
    for entry in data:
        by_group = {}
        for key, val in entry.items():
            grp, _, tag = key.rpartition(":")
            if tag in ("GPSLatitude", "GPSLongitude", "GPSDateTime", "GPSMeasureMode"):
                by_group.setdefault(grp, {})[tag] = val
        for grp, tags in by_group.items():
            lat, lon, ts = tags.get("GPSLatitude"), tags.get("GPSLongitude"), tags.get("GPSDateTime")
            fix = tags.get("GPSMeasureMode")
            if lat is None or lon is None or not ts:
                continue
            if fix is not None and re.match(r"^0\b|no.?fix", str(fix).strip(), re.IGNORECASE):
                continue  # GoPro reports "no fix" (GPSF=0) for some samples, mostly at the start
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                continue
            if lat_f == 0.0 and lon_f == 0.0:
                continue  # no-fix placeholder
            samples.append((grp, lat_f, lon_f, ts))

    def sort_key(item):
        m = re.search(r"(\d+)", item[0])
        return int(m.group(1)) if m else 0

    samples.sort(key=sort_key)
    return samples


def pick_consensus_sample(samples, cluster_precision=2):
    """A GPS receiver can report noisy, wildly-varying positions for a while after
    startup before it settles on a real lock -- for a camera with a poor sky view
    (e.g. a rear-facing mount) this acquisition period can run for a minute or more,
    long enough to span most/all of a short clip, not just the first sample or two.
    Rather than trusting sample #1 (or an early window that might still be inside
    the noisy period), cluster ALL samples in the clip by rounded position and take
    the earliest sample from whichever cluster the most of them agree on -- scattered
    acquisition noise loses to a real, self-consistent fix regardless of how long the
    noise lasts."""
    buckets = {}
    for s in samples:
        _, lat, lon, _ = s
        key = (round(lat, cluster_precision), round(lon, cluster_precision))
        buckets.setdefault(key, []).append(s)
    best = max(buckets.values(), key=len)
    return best[0]


def parse_gps_datetime_utc(s: str):
    """GPSDateTime from exiftool is always UTC."""
    if not s:
        return None
    m = re.match(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", s.strip())
    if not m:
        return None
    y, mo, d, h, mi, se = map(int, m.groups())
    try:
        return datetime(y, mo, d, h, mi, se, tzinfo=timezone.utc)
    except ValueError:
        return None  # e.g. exiftool's "0000:00:00 00:00:00" unknown-date sentinel


def parse_camera_datetime(s: str):
    """CreateDate/CreationDate as written by the camera. May carry a timezone
    offset; if not, treat it as naive local (no reliable tz info available)."""
    if not s:
        return None
    s = s.strip()
    m = re.match(
        r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$", s
    )
    if not m:
        return None
    y, mo, d, h, mi, se, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se))
    except ValueError:
        return None  # e.g. exiftool's "0000:00:00 00:00:00" unknown-date sentinel
    if not tz:
        return dt
    if tz == "Z":
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    offset = sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return dt.replace(tzinfo=timezone(timedelta(seconds=offset)))


def extract_gps_and_time(path: Path):
    """Returns (lat, lon, utc_datetime) from the first GPS fix in the clip,
    or (None, None, best_effort_datetime) if the clip has no GPS data."""
    samples = extract_gps_samples(path)
    if samples:
        _, lat, lon, ts = pick_consensus_sample(samples)
        dt = parse_gps_datetime_utc(ts)
        if dt:
            return lat, lon, dt

    try:
        info = get_exiftool_json(path, ["-c", "%+.6f", "-GPSLatitude", "-GPSLongitude", "-GPSDateTime"])
        lat, lon, ts = info.get("GPSLatitude"), info.get("GPSLongitude"), info.get("GPSDateTime")
        if lat is not None and lon is not None and ts:
            dt = parse_gps_datetime_utc(str(ts))
            if dt:
                return float(lat), float(lon), dt
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass

    try:
        info = get_exiftool_json(path, ["-CreationDate", "-CreateDate", "-MediaCreateDate"])
        for key in ("CreationDate", "CreateDate", "MediaCreateDate"):
            if info.get(key):
                dt = parse_camera_datetime(str(info[key]))
                if dt:
                    return None, None, dt
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass

    return None, None, None


# --------------------------------------------------------------------------
# Reverse geocoding / timezone (fully offline)
# --------------------------------------------------------------------------

RACETRACKS_PATH = Path(__file__).parent / "data" / "racetracks.json"
_racetracks = None


def load_racetracks():
    global _racetracks
    if _racetracks is None:
        try:
            _racetracks = json.loads(RACETRACKS_PATH.read_text())
        except FileNotFoundError:
            _racetracks = []
    return _racetracks


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * r * asin(sqrt(a))


def racetrack_for(lat: float, lon: float, max_km: float):
    """Nearest known racetrack (from data/racetracks.json, sourced from Wikidata)
    within max_km, or None. Wikidata's coordinate for a track is usually a single
    reference point (e.g. start/finish line), not the full circuit outline, so
    max_km needs enough slack to cover being anywhere on track -- large circuits
    (e.g. the Nordschleife) need a bigger radius than short ones."""
    best_name, best_dist = None, None
    for t in load_racetracks():
        d = haversine_km(lat, lon, t["lat"], t["lon"])
        if d <= max_km and (best_dist is None or d < best_dist):
            best_name, best_dist = t["name"], d
    return best_name


def city_for(lat: float, lon: float) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = rg.search((lat, lon))
    return result[0]["name"]


def place_for(lat: float, lon: float, track_radius_km: float) -> str:
    if track_radius_km > 0:
        track = racetrack_for(lat, lon, track_radius_km)
        if track:
            return track
    return city_for(lat, lon)


def utc_to_local(dt_utc: datetime, lat: float, lon: float) -> datetime:
    tzname = _tf.timezone_at(lat=lat, lng=lon)
    if not tzname:
        return dt_utc
    try:
        return dt_utc.astimezone(ZoneInfo(tzname))
    except Exception:
        return dt_utc


def sanitize_filename_part(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s.strip())
    return s or "UnknownLocation"


COMPLETE_DURATION_TOLERANCE = 2.0  # seconds


def total_duration(chapters):
    total = 0.0
    for p in chapters:
        d = probe_duration(p)
        if d is None:
            return None
        total += d
    return total


def check_existing(path: Path, chapters):
    """Tell apart three reasons a file might already sit at a computed output path:
    our own complete output from an earlier run ("complete" -- skip), a truncated
    leftover from a crash partway through encoding this same clip ("incomplete" --
    redo), or an unrelated file that just happens to share the computed name
    ("collision" -- leave it alone, name this clip's output something else)."""
    expected = total_duration(chapters)
    actual = probe_duration(path)
    if actual is None or expected is None:
        return "incomplete"  # unreadable/corrupt -- not something we'd ever want to keep
    if actual > expected + COMPLETE_DURATION_TOLERANCE:
        return "collision"  # longer than our own chapters could ever produce
    if actual < expected - COMPLETE_DURATION_TOLERANCE:
        return "incomplete"
    return "complete"


def resolve_output(base_path: Path, chapters):
    """Picks the output path for a clip and says what to do with it: 'new' (no file
    there yet), 'complete' (already fully encoded, skip), or 'incomplete' (redo --
    ffmpeg's -y will overwrite it). Genuine naming collisions with an unrelated clip
    still get the old -2/-3 numbered fallback instead of touching that file."""
    candidate = base_path
    i = 2
    while candidate.exists():
        status = check_existing(candidate, chapters)
        if status != "collision":
            return candidate, status
        candidate = base_path.with_name(f"{base_path.stem}-{i}{base_path.suffix}")
        i += 1
    return candidate, "new"


# --------------------------------------------------------------------------
# ffmpeg concat + transcode
# --------------------------------------------------------------------------

def has_audio(path: Path) -> bool:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a",
           "-show_entries", "stream=index", "-of", "csv=p=0", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    return bool(out.stdout.strip())


# Fit within 1080p without upscaling smaller sources, preserving aspect ratio and
# keeping dimensions even (required by H.264).
QUICKTIME_SCALE_FILTER = (
    "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
)


def transcode(chapters, out_path: Path, args, dry_run: bool = False):
    n = len(chapters)
    audio_ok = all(has_audio(p) for p in chapters)
    scale = args.format == "quicktime"

    cmd = ["ffmpeg", "-y"]
    for p in chapters:
        cmd += ["-i", str(p)]

    if n > 1:
        video_label = "vc" if scale else "v"
        if audio_ok:
            filt = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n)) + f"concat=n={n}:v=1:a=1[{video_label}][a]"
        else:
            filt = "".join(f"[{i}:v:0]" for i in range(n)) + f"concat=n={n}:v=1:a=0[{video_label}]"
        if scale:
            filt += f";[{video_label}]{QUICKTIME_SCALE_FILTER}[v]"
        cmd += ["-filter_complex", filt, "-map", "[v]"]
        if audio_ok:
            cmd += ["-map", "[a]"]
    else:
        cmd += ["-map", "0:v:0"]
        if scale:
            cmd += ["-vf", QUICKTIME_SCALE_FILTER]
        if audio_ok:
            cmd += ["-map", "0:a:0"]

    if args.encoder == "videotoolbox":
        # Apple hardware H.264 encoder (VideoToolbox): much faster and far less CPU/heat
        # than software x264, at some cost to compression efficiency at equal quality.
        cmd += ["-c:v", "h264_videotoolbox", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "libx264", "-preset", args.preset, "-pix_fmt", "yuv420p"]

    if args.format == "quicktime":
        # Matches QuickTime's own "export 1080p" preset: ~16 Mbit/s target bitrate
        # rather than a quality scale.
        cmd += ["-b:v", args.bitrate]
    elif args.encoder == "videotoolbox":
        # -q:v is a 1-100 quality scale, higher = better/larger (opposite of x264's CRF).
        cmd += ["-q:v", str(args.quality)]
    else:
        cmd += ["-crf", str(args.crf)]

    if audio_ok:
        if args.format == "quicktime":
            cmd += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "320k"]
    cmd += ["-movflags", "+faststart", str(out_path)]

    if dry_run:
        print("  would run:", " ".join(cmd))
        return
    subprocess.run(cmd, check=True)


# --------------------------------------------------------------------------
# Optional: pull files straight from the camera via gphoto2
# --------------------------------------------------------------------------

def import_from_camera(dest: Path = None) -> Path:
    if shutil.which("gphoto2") is None:
        sys.exit(
            "error: --from-camera requires gphoto2 (brew install libgphoto2 gphoto2).\n"
            "GoPro/PTP support in gphoto2 varies by camera model and can be flaky --\n"
            "if it doesn't detect your camera, just import via Image Capture as usual\n"
            "and pass that destination folder as the source instead."
        )
    dest = dest or Path(tempfile.mkdtemp(prefix="gopro_import_"))
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Importing files from camera via gphoto2 into {dest} ...")
    cmd = ["gphoto2", "--get-all-files", "--skip-existing", "--filename", str(dest / "%f")]
    subprocess.run(cmd, check=True, cwd=dest)
    return dest


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def process_group(clip_id, chapters, args):
    names = ", ".join(p.name for p in chapters)
    print(f"Clip {clip_id}: {names}")

    first = chapters[0]
    lat, lon, dt = extract_gps_and_time(first)

    if lat is not None and lon is not None:
        place = place_for(lat, lon, args.track_radius_km)
        local_dt = utc_to_local(dt, lat, lon)
    else:
        place = "UnknownLocation"
        local_dt = dt
        print(f"  warning: no GPS fix found in {first.name}; using camera clock "
              f"timestamp with no timezone correction", file=sys.stderr)

    if local_dt is None:
        local_dt = datetime.fromtimestamp(first.stat().st_mtime)
        print(f"  warning: no usable timestamp found for {first.name}; "
              f"falling back to file mtime", file=sys.stderr)

    date_str = local_dt.strftime("%Y-%m-%d")
    time_str = local_dt.strftime("%H-%M")
    base_name = f"{sanitize_filename_part(place)}_{date_str}_{time_str}"
    base_path = args.outdir / f"{base_name}.mp4"
    out_path, status = resolve_output(base_path, chapters)

    coord_info = f"lat={lat:.5f}, lon={lon:.5f}" if lat is not None else "no GPS"

    if status == "complete":
        print(f"  -> {out_path.name}  already complete, skipping  ({coord_info})")
        return
    if status == "incomplete":
        print(f"  -> {out_path.name}  found incomplete (likely a previous crash); "
              f"re-encoding  ({coord_info})")
    else:
        print(f"  -> {out_path.name}  ({coord_info})")

    if not args.list_only:
        transcode(chapters, out_path, args, dry_run=args.dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, nargs="?",
                     help="Folder containing GoPro clips (Image Capture import folder, "
                          "or a mounted camera's DCIM/100GOPRO folder)")
    ap.add_argument("-o", "--outdir", type=Path, default=None,
                     help="Output folder (default: <source>/Exported)")
    ap.add_argument("--track-radius-km", type=float, default=5.0,
                     help="If the GPS fix is within this many km of a known racetrack "
                          "(bundled, from Wikidata), name the file after the track instead "
                          "of the nearest city. Set to 0 to always use the nearest city "
                          "(default: 5.0)")
    ap.add_argument("--format", choices=["original", "quicktime"], default="original",
                     help="'original' keeps the source resolution and a quality-based encode "
                          "(default). 'quicktime' matches QuickTime's own 1080p export preset: "
                          "scales down to fit 1920x1080 (never upscales) at a fixed --bitrate, "
                          "with 48kHz stereo AAC audio")
    ap.add_argument("--bitrate", default="16M",
                     help="target video bitrate for --format quicktime (default: 16M)")
    ap.add_argument("--encoder", choices=["videotoolbox", "libx264"], default="videotoolbox",
                     help="'videotoolbox' uses Apple hardware H.264 encoding (fast, default). "
                          "'libx264' uses software encoding (slower, slightly better compression "
                          "efficiency at equal quality)")
    ap.add_argument("--quality", type=int, default=65,
                     help="videotoolbox quality, 1-100, higher = better/larger "
                          "(default: 65, --format original only)")
    ap.add_argument("--crf", type=int, default=18,
                     help="libx264 quality, lower = better/larger "
                          "(default: 18, --format original + --encoder libx264 only)")
    ap.add_argument("--preset", default="slow",
                     help="libx264 encoding preset (default: slow, libx264 only)")
    ap.add_argument("--list-only", action="store_true",
                     help="Only show detected clip groups and planned filenames; don't encode")
    ap.add_argument("--dry-run", action="store_true",
                     help="Print the ffmpeg commands instead of running them")
    ap.add_argument("--from-camera", action="store_true",
                     help="Experimental: pull files directly from a connected camera via "
                          "gphoto2 instead of reading a folder")
    args = ap.parse_args()

    check_tool("ffmpeg")
    check_tool("ffprobe")
    check_tool("exiftool")

    if args.from_camera:
        args.source = import_from_camera()
    elif args.source is None:
        ap.error("source folder is required unless --from-camera is used")

    if not args.source.is_dir():
        sys.exit(f"error: {args.source} is not a directory")

    args.outdir = args.outdir or (args.source / "Exported")
    args.outdir.mkdir(parents=True, exist_ok=True)

    groups = find_clip_groups(args.source)
    if not groups:
        sys.exit(f"No GoPro clips (e.g. GH010369.MP4 / GX010369.MP4) found in {args.source}")

    final_groups = []
    for clip_id, chapters in groups:
        final_groups.extend((clip_id, sub) for sub in split_on_continuity(clip_id, chapters))

    print(f"Found {len(final_groups)} clip(s) in {args.source}\n")
    for clip_id, chapters in final_groups:
        process_group(clip_id, chapters, args)
        print()


if __name__ == "__main__":
    main()
