#!/usr/bin/env python3
"""
youtube-shorts-auto-clipper
===========================
Give it a YouTube video URL and it downloads the video, then automatically
cuts it into vertical 9:16 Shorts clips (<60s each) with burned-in
word-by-word captions, ready to upload.

Pipeline:
    1. Download with yt-dlp (best video+audio, mp4, max 1080p).
    2. Transcribe with faster-whisper (word timestamps, CPU-friendly).
    3. Segment into candidate clips (30-59s by default) using transcript
       gaps, ranked by a VIRAL-POTENTIAL score: hook in the first 3s,
       energy peaks, strong/emotional statements, payoff inside the clip,
       speech density. Non-overlapping, best N first. Falls back to evenly
       spaced time chunks when there is no speech.
       The score is a HEURISTIC only — it finds moments with higher viral
       potential; it never guarantees views. Real virality also depends on
       the algorithm, timing, audience and luck.
    4. Reframe to 1080x1920 (center crop) and burn in karaoke-style
       word-by-word captions (ASS subtitles) with ffmpeg.
    5. Write output/clip_01.mp4 ... + clips.json manifest.

Usage:
    python clip.py <youtube_url> [--num-clips 3] [--min-len 30] [--max-len 59]
               [--lang id] [--model base] [--no-captions]
               [--timestamps "1:05-1:50,3:00-3:45"]

Only use videos you own or that are public-domain / Creative Commons.
Downloading other people's videos may violate YouTube's Terms of Service.
"""

import argparse
import json
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import yt_dlp
from faster_whisper import WhisperModel

OUT_W, OUT_H = 1080, 1920
GAP_MIN = 0.8          # seconds of silence that count as a cut point
WORDS_PER_LINE = 5     # max words per caption line
LINE_MAX_SEC = 2.5     # max seconds per caption line
AUDIO_SR = 16000       # whisper sample rate
ENERGY_WIN = 0.5       # seconds per loudness window

# --- Viral-potential signals (bilingual: Indonesian + English) -------------
# Hook words: questions and curiosity gaps that grab attention in 3 seconds.
HOOK_QUESTION = {
    "kenapa", "mengapa", "bagaimana", "gimana", "apa", "apakah", "siapa",
    "kapan", "dimana", "di mana", "berapa", "kok", "why", "how", "what",
    "who", "when", "where", "which", "did you know",
}
HOOK_CURIOSITY = {
    "ternyata", "rahasia", "jangan", "stop", "tunggu", "bayangkan", "bayangin",
    "fakta", "salah", "bohong", "gratis", "viral", "cuma", "hanya",
    "diam-diam", "tersembunyi", "jangan sampai", "secret", "wait", "imagine",
    "truth", "never", "actually", "turns out", "lies", "free", "hidden",
    "shocking", "unbelievable", "warning", "nobody tells", "stop doing",
}
STRONG_CLAIM = {
    "terbaik", "terburuk", "paling", "pasti", "tidak pernah", "selalu",
    "semua orang", "semua", "tidak ada", "mustahil", "gila", "luar biasa",
    "best", "worst", "never", "always", "everyone", "nobody", "impossible",
    "insane", "incredible", "greatest", "everybody", "hate", "love",
}
PAYOFF_WORDS = {
    "jadi", "akhirnya", "kesimpulannya", "itulah", "makanya", "so",
    "that's why", "in the end", "finally", "therefore", "that's how",
    "turns out", "and that's", "hasilnya",
}
FILLER = {
    "eh", "um", "anu", "ya", "gitu", "sih", "dong", "loh", "nah", "like",
    "you know", "uh", "hmm", "ehm", "kan", "umm", "err",
}
LAUGHTER = {"[laughter]", "(tertawa)", "haha", "wkwk", "lol", "hehe"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def parse_timestamp(text):
    """'1:05', '1:05:30' or '65' -> seconds (float)."""
    text = text.strip()
    if ":" in text:
        parts = [float(p) for p in text.split(":")]
        total = 0.0
        for p in parts:
            total = total * 60 + p
        return total
    return float(text)


def fmt_time(sec):
    """Seconds -> 'M:SS' for display."""
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def parse_ranges(text):
    """'1:05-1:50,3:00-3:45' -> [(65.0, 110.0), (180.0, 225.0)]."""
    ranges = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            raise ValueError(f"bad range (need start-end): {chunk!r}")
        start_s, end_s = chunk.split("-", 1)
        start, end = parse_timestamp(start_s), parse_timestamp(end_s)
        if end <= start:
            raise ValueError(f"bad range (end <= start): {chunk!r}")
        ranges.append((start, end))
    if not ranges:
        raise ValueError("no valid ranges in --timestamps")
    return ranges


def run(cmd, cwd=None, quiet=False):
    """Run a command, print it (unless quiet), raise on failure."""
    if not quiet:
        print("  $ " + " ".join(str(c) for c in cmd))
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=quiet)


def ffprobe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# --------------------------------------------------------------------------
# 1. download
# --------------------------------------------------------------------------

def download_video(url, workdir):
    """Download best mp4 (<=1080p) with yt-dlp. Returns (path, title, id).

    Tries the default (web) player client first, then falls back to the
    Android client: datacenter IPs often hit YouTube's "Sign in to confirm
    you're not a bot" (HTTP 429) with the web client, while the Android
    client still works (sometimes at lower quality).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    base_opts = {
        "format": ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
                   "bv*[height<=1080]+ba/b[ext=mp4]/b"),
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    print("[1/5] Downloading with yt-dlp ...")
    attempts = [
        ("web (default)", {}),
        ("android", {"extractor_args": {"youtube": {"player_client": ["android"]}}}),
    ]
    last_error = None
    for label, extra in attempts:
        opts = dict(base_opts)
        opts.update(extra)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_id = info["id"]
                title = info.get("title", video_id)
            path = next(workdir.glob(f"{video_id}.*"))
            print(f"      {title}  ({path.name})  [client: {label}]")
            return path, title, video_id
        except Exception as exc:  # noqa: BLE001 - retry with next client
            last_error = exc
            print(f"      client {label} failed ({exc}), trying next ...")
    print("ERROR: download failed with every player client.")
    print("      YouTube may be rate-limiting this IP (HTTP 429 / bot check).")
    print("      Try again later, or pass your own browser cookies:")
    print("        yt-dlp --cookies-from-browser chrome <url>")
    raise SystemExit(1) from last_error


# --------------------------------------------------------------------------
# 2. transcribe
# --------------------------------------------------------------------------

def extract_audio_array(path, workdir):
    """Extract 16kHz mono audio with ffmpeg and return it as a float32
    numpy array in [-1, 1]. Feeding faster-whisper an array (instead of a
    file path) avoids its PyAV decoder, which breaks on newer `av`
    versions (faster-whisper calls av.open(metadata_errors=...), removed
    in av>=15)."""
    wav_path = workdir / "audio-16k.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(AUDIO_SR), "-f", "wav", str(wav_path)],
        quiet=True)
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def energy_curve(audio, sr=AUDIO_SR, window_sec=ENERGY_WIN):
    """Per-window RMS loudness. Returns (times, rms) numpy arrays."""
    n = int(sr * window_sec)
    count = len(audio) // n
    if count == 0:
        return np.array([]), np.array([])
    trimmed = audio[:count * n].reshape(count, n)
    rms = np.sqrt(np.mean(trimmed ** 2, axis=1))
    times = np.arange(count) * window_sec
    return times, rms


def transcribe(path, model_name, lang, workdir):
    """Transcribe with faster-whisper. Returns (words, energy) where
    energy is (times, rms) loudness windows for the viral scoring."""
    print(f"[2/5] Transcribing (faster-whisper {model_name}, lang={lang}) ...")
    audio = extract_audio_array(path, workdir)
    times, rms = energy_curve(audio)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(
        audio, language=lang, word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    words = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({"start": w.start, "end": w.end,
                          "word": w.word.strip()})
    words = [w for w in words if w["word"]]
    print(f"      {len(words)} words recognised")
    return words, (times, rms)


# --------------------------------------------------------------------------
# 3. segmentation
# --------------------------------------------------------------------------

def _cut_points(words, duration):
    """Silence gaps (>= GAP_MIN) are good cut points."""
    points = [0.0, duration]
    for a, b in zip(words, words[1:]):
        if b["start"] - a["end"] >= GAP_MIN:
            points.append(a["end"])
    return sorted(set(points))


def _speech_seconds(words, start, end):
    total = 0.0
    for w in words:
        lo, hi = max(w["start"], start), min(w["end"], end)
        if hi > lo:
            total += hi - lo
    return total


def _matches(text, phrases):
    """True if any phrase matches: multi-word phrases by substring,
    single words by whole-word match (case-insensitive)."""
    low = text.lower()
    words = set(re.findall(r"[a-z0-9']+", low))
    for p in phrases:
        if " " in p:
            if p in low:
                return True
        elif p in words:
            return True
    return False


def _count_matches(text, phrases):
    low = text.lower()
    words = set(re.findall(r"[a-z0-9']+", low))
    n = 0
    for p in phrases:
        if " " in p:
            if p in low:
                n += 1
        elif p in words:
            n += 1
    return n


def extract_hook(words, start, end):
    """The hook sentence: first sentence containing a hook word, else the
    first sentence of the clip. Short, for clips.json."""
    clip_words = [w["word"] for w in words
                  if w["end"] > start and w["start"] < end]
    if not clip_words:
        return ""
    text = " ".join(clip_words)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    for s in sentences:
        if _matches(s, HOOK_QUESTION) or _matches(s, HOOK_CURIOSITY):
            return s[:90] + ("..." if len(s) > 90 else "")
    first = sentences[0] if sentences else text
    return first[:90] + ("..." if len(first) > 90 else "")


def score_clip(words, start, end, energy):
    """Viral-potential score (0-100) for one candidate clip.

    HEURISTIC ONLY — it ranks moments by hook/energy/payoff signals.
    It never guarantees views: real virality also depends on the
    algorithm, timing, audience and luck.
    """
    times, rms = energy
    clip_words = [w for w in words if w["end"] > start and w["start"] < end]
    text = " ".join(w["word"] for w in clip_words)
    first3 = [w for w in clip_words if w["start"] < start + 3.0]
    first3_text = " ".join(w["word"] for w in first3)
    last10 = [w for w in clip_words if w["start"] >= end - 10.0]
    last10_text = " ".join(w["word"] for w in last10)
    duration = max(end - start, 0.1)

    # 1. Hook in the first 3 seconds (max 30)
    hook = 0
    if _matches(first3_text, HOOK_QUESTION):
        hook += 15
    if _matches(first3_text, HOOK_CURIOSITY):
        hook += 15
    if "!" in first3_text:
        hook += 5
    if _matches(first3_text, STRONG_CLAIM):
        hook += 5
    hook = min(hook, 30)

    # 2. Energy peaks: loud moments + dynamic (non-flat) audio (max 25)
    energy_score = 0
    if len(rms):
        gmean, gstd = float(np.mean(rms)), float(np.std(rms))
        mask = (times >= start) & (times < end)
        clip_rms = rms[mask]
        peaks = int(np.sum(clip_rms > gmean + gstd)) if len(clip_rms) else 0
        energy_score = min(peaks, 5) * 4
        var = float(np.std(clip_rms)) / (float(np.mean(clip_rms)) + 1e-9) \
            if len(clip_rms) else 0.0
        if var > 0.5:
            energy_score += 5
        # 4. Payoff: energy peak in the last third (max 5, part of payoff)
        third = start + 2 * duration / 3
        last_mask = (times >= third) & (times < end)
        if len(clip_rms) and np.any(rms[last_mask] > gmean + gstd):
            payoff_energy = 5
        else:
            payoff_energy = 0
    else:
        var, payoff_energy = 0.0, 0
    energy_score = min(energy_score, 25)

    # 3. Strong / emotional statements, hot takes (max 20)
    statements = min(_count_matches(text, STRONG_CLAIM), 4) * 4
    if _matches(text, LAUGHTER) or "!" in text:
        statements += 4
    statements = min(statements, 20)

    # 4. Payoff / punchline inside the clip (max 15)
    payoff = payoff_energy
    if _matches(last10_text, PAYOFF_WORDS):
        payoff += 10
    payoff = min(payoff, 15)

    # 5. Speech density (max 10)
    density = _speech_seconds(words, start, end) / duration
    density_score = round(density * 10)

    score = hook + energy_score + statements + payoff + density_score

    # Penalties
    if hook == 0:
        score -= 15  # no clear hook in the first 3 seconds
    if clip_words:
        filler_ratio = sum(1 for w in clip_words
                           if w["word"].lower().strip(".,!?") in FILLER) \
            / len(clip_words)
        if filler_ratio > 0.35:
            score -= 10  # filler-only segment
    if len(rms) and var < 0.2:
        score -= 10  # long flat / monotone stretch

    return max(0, min(100, int(round(score))))


def segment_auto(words, duration, min_len, max_len, num_clips, energy):
    """Candidate clips from transcript gaps, ranked by viral-potential
    score (hook + energy + statements + payoff + speech density)."""
    if not words:
        return []
    cuts = _cut_points(words, duration)
    candidates = []
    for start in cuts:
        # latest cut point that keeps the clip within [min_len, max_len]
        ends = [c for c in cuts if start + min_len <= c <= start + max_len]
        if ends:
            end = max(ends)
        else:
            # no gap in range: end at max_len if speech reaches min_len
            later = [w["end"] for w in words if w["start"] >= start]
            if not later:
                continue  # no speech at/after this start point
            last_word_end = max(later)
            if last_word_end < start + min_len:
                continue
            end = min(start + max_len, last_word_end)
        score = score_clip(words, start, end, energy)
        candidates.append({
            "start": start, "end": end,
            "viral_score": score,
            "hook": extract_hook(words, start, end),
        })
    # best first, greedily take non-overlapping
    candidates.sort(key=lambda c: (-c["viral_score"],
                                   -(c["end"] - c["start"])))
    picked, used = [], []
    for c in candidates:
        if any(c["start"] < u["end"] and c["end"] > u["start"] for u in used):
            continue
        picked.append(c)
        used.append(c)
        if len(picked) >= num_clips:
            break
    return sorted(picked, key=lambda c: c["start"])


def segment_by_time(duration, min_len, max_len, num_clips, energy=None):
    """Fallback when there is no speech: evenly spaced chunks, scored
    only on loud moments (modest scores — no words means no hook)."""
    clips, start = [], 0.0
    times, rms = energy if energy is not None else (np.array([]), np.array([]))
    gmean = float(np.mean(rms)) if len(rms) else 0.0
    gstd = float(np.std(rms)) if len(rms) else 0.0
    while len(clips) < num_clips and start + min_len <= duration:
        end = min(start + max_len, duration)
        peaks = 0
        if len(rms):
            mask = (times >= start) & (times < end)
            peaks = int(np.sum(rms[mask] > gmean + gstd))
        clips.append({"start": start, "end": end,
                      "viral_score": min(40, 10 + peaks * 5), "hook": ""})
        start = end
    return clips


# --------------------------------------------------------------------------
# 4. captions (ASS, karaoke word-by-word)
# --------------------------------------------------------------------------

def _ass_time(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(words, clip_start, clip_end, ass_path):
    """Write an ASS file: lines of up to WORDS_PER_LINE words, karaoke tags
    highlight the word being spoken (yellow) while the rest stays white."""
    clip_words = [w for w in words
                  if w["end"] > clip_start and w["start"] < clip_end]
    lines = []
    cur = []
    for w in clip_words:
        cur.append(w)
        if (len(cur) >= WORDS_PER_LINE
                or (cur and w["end"] - cur[0]["start"] >= LINE_MAX_SEC)):
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Shorts,Noto Sans,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,2,60,60,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for line in lines:
        start = max(0.0, line[0]["start"] - clip_start)
        end = max(0.0, line[-1]["end"] - clip_start) + 0.15
        parts = []
        for w in line:
            dur_cs = max(5, int(round((w["end"] - w["start"]) * 100)))
            parts.append("{\\k%d}%s" % (dur_cs, w["word"]))
        text = " ".join(parts)
        events.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Shorts,,0,0,0,,{text}"
        )
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return len(lines)


# --------------------------------------------------------------------------
# 5. render one clip
# --------------------------------------------------------------------------

def render_clip(src, start, end, ass_name, out_path, out_dir):
    """Center-crop to 9:16, scale to 1080x1920, burn in captions.
    Input/output paths are made absolute: ffmpeg runs with cwd=out_dir
    so the ASS filename can stay relative."""
    src = Path(src).resolve()
    out_path = Path(out_path).resolve()
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if ass_name:
        vf += f",ass={ass_name}"
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.2f}", "-to", f"{end:.2f}",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    run(cmd, cwd=out_dir)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def suggest_title(words, clip_start, clip_end, fallback):
    clip_words = [w["word"] for w in words
                  if w["end"] > clip_start and w["start"] < clip_end][:8]
    if clip_words:
        text = " ".join(clip_words).strip().capitalize()
        return text + (" ..." if len(clip_words) == 8 else "")
    return fallback


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="clip.py",
        description="Download a YouTube video and auto-cut it into vertical "
                    "9:16 Shorts clips (<60s) with burned-in captions. "
                    "Clips are ranked by a viral-potential score (a heuristic "
                    "based on hooks, energy and payoff — never a guarantee "
                    "of views).",
    )
    ap.add_argument("url", help="YouTube video URL")
    ap.add_argument("--num-clips", type=int, default=3,
                    help="max clips to produce (default: 3)")
    ap.add_argument("--min-len", type=float, default=30,
                    help="minimum clip length in seconds (default: 30)")
    ap.add_argument("--max-len", type=float, default=59,
                    help="maximum clip length in seconds (default: 59)")
    ap.add_argument("--lang", default="id",
                    help="transcription language code, e.g. id, en (default: id)")
    ap.add_argument("--model", default="base",
                    help="faster-whisper model: tiny, base, small, medium "
                         "(default: base)")
    ap.add_argument("--no-captions", action="store_true",
                    help="skip burned-in captions")
    ap.add_argument("--timestamps",
                    help='manual clip ranges, overrides auto-segmentation, '
                         'e.g. "1:05-1:50,3:00-3:45"')
    ap.add_argument("--workdir", default="downloads",
                    help="folder for the downloaded source video "
                         "(default: downloads)")
    ap.add_argument("--out", default="output",
                    help="folder for the finished clips (default: output)")
    args = ap.parse_args(argv)

    if args.max_len > 60:
        print("WARNING: --max-len > 60s is not a YouTube Short; clamping to 60.")
        args.max_len = 60.0
    if args.min_len > args.max_len:
        ap.error("--min-len must be <= --max-len")

    workdir = Path(args.workdir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. download
    src, title, _vid = download_video(args.url, workdir)
    duration = ffprobe_duration(src)
    print(f"      duration {fmt_time(duration)}")

    # 2. transcribe (skip when manual ranges + no captions)
    need_transcript = not (args.timestamps and args.no_captions)
    if need_transcript:
        words, energy = transcribe(src, args.model, args.lang, workdir)
        (out_dir / "transcript.json").write_text(
            json.dumps(words, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        words, energy = [], (np.array([]), np.array([]))

    # 3. segment
    print("[3/5] Segmenting into clips (ranked by viral-potential score) ...")
    if args.timestamps:
        ranges = parse_ranges(args.timestamps)
        clips = []
        for start, end in ranges:
            if end - start > args.max_len:
                print(f"      WARNING: range {fmt_time(start)}-{fmt_time(end)} "
                      f"longer than --max-len, clamping to {args.max_len:.0f}s")
                end = start + args.max_len
            if end > duration:
                print(f"      WARNING: range end {fmt_time(end)} past video "
                      f"end {fmt_time(duration)}, skipping")
                continue
            clips.append({
                "start": start, "end": end,
                "viral_score": score_clip(words, start, end, energy)
                if words else 0,
                "hook": extract_hook(words, start, end) if words else "",
            })
        clips = clips[: args.num_clips]
    else:
        clips = segment_auto(words, duration, args.min_len,
                             args.max_len, args.num_clips, energy)
        if not clips:
            if words:
                print("      no good speech segments found, "
                      "falling back to time chunks")
            else:
                print("      no speech detected, falling back to time chunks")
            clips = segment_by_time(duration, args.min_len,
                                    args.max_len, args.num_clips, energy)
    if not clips:
        print("ERROR: could not find any clip. The video may be shorter "
              "than --min-len.")
        return 1
    for i, c in enumerate(clips, 1):
        print(f"      clip {i}: {fmt_time(c['start'])} - {fmt_time(c['end'])} "
              f"({c['end'] - c['start']:.0f}s, "
              f"viral-potential {c['viral_score']}/100)")
        if c.get("hook"):
            print(f"              hook: {c['hook']}")

    # 4+5. render + manifest
    print("[4/5] Rendering clips (center crop 1080x1920 + captions) ...")
    manifest = {
        "source_url": args.url,
        "source_title": title,
        "source_duration_sec": round(duration, 1),
        "model": args.model,
        "lang": args.lang,
        "captions": not args.no_captions,
        "scoring": ("viral-potential score 0-100: a heuristic based on "
                    "hook, energy, statements and payoff. It ranks moments "
                    "with higher viral potential; it never guarantees views."),
        "clips": [],
    }
    for i, c in enumerate(clips, 1):
        name = f"clip_{i:02d}"
        out_path = out_dir / f"{name}.mp4"
        ass_name = None
        if not args.no_captions and words:
            ass_path = out_dir / f"{name}.ass"
            n_lines = build_ass(words, c["start"], c["end"], ass_path)
            ass_name = ass_path.name
            print(f"      {name}: {n_lines} caption lines")
        elif not args.no_captions:
            print(f"      {name}: no words in range, no captions burned in")
        render_clip(src, c["start"], c["end"], ass_name, out_path, out_dir)
        dur = ffprobe_duration(out_path)
        manifest["clips"].append({
            "file": out_path.name,
            "start_sec": round(c["start"], 1),
            "end_sec": round(c["end"], 1),
            "duration_sec": round(dur, 1),
            "viral_score": c["viral_score"],
            "hook": c.get("hook", ""),
            "title": suggest_title(words, c["start"], c["end"],
                                   f"Clip {i} - {title}"),
        })
        print(f"      {name}.mp4  {dur:.1f}s  1080x1920  "
              f"(viral-potential {c['viral_score']}/100)")

    manifest_path = out_dir / "clips.json"
    manifest_path.write_text(json.dumps(manifest, indent=2,
                                        ensure_ascii=False), encoding="utf-8")
    print(f"[5/5] Done. Manifest: {manifest_path}")
    for c in manifest["clips"]:
        print(f"      {c['file']}  {c['duration_sec']}s  — {c['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
