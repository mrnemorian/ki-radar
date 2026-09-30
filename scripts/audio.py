"""Podcast audio: Azure dialogue synthesis (Piper fallback), generated jingle, mastering."""
import array
import math
import os
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave
from xml.sax.saxutils import escape

SR = 44100
HOSTS = ("Seraphina", "Florian")
AZURE_VOICES = {
    "Seraphina": os.environ.get("AZURE_VOICE_A", "de-DE-SeraphinaMultilingualNeural"),
    "Florian": os.environ.get("AZURE_VOICE_B", "de-DE-FlorianMultilingualNeural"),
}
EN_TAG = re.compile(r"\[en\](.+?)\[/en\]", re.S)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


# ---------------------------------------------------------------- jingle
def _pluck(buf, start, freq, dur, amp):
    n0, n = int(start * SR), int(dur * SR)
    for i in range(n):
        j = n0 + i
        if j >= len(buf):
            break
        t = i / SR
        env = min(1.0, t / 0.006) * math.exp(-t / 0.45)
        w = 2 * math.pi * freq * t
        buf[j] += amp * env * (math.sin(w) + 0.35 * math.sin(2 * w) + 0.12 * math.sin(3 * w))


def jingle(path, notes, length):
    """Write a short mono WAV: plucked notes + soft echo + fade-out. No external samples."""
    buf = [0.0] * int(length * SR)
    for start, freq, dur, amp in notes:
        _pluck(buf, start, freq, dur, amp)
    delay = int(0.19 * SR)
    for i in range(delay, len(buf)):
        buf[i] += 0.28 * buf[i - delay]
    peak = max(abs(x) for x in buf) or 1.0
    fade = int(0.5 * SR)
    pcm = array.array("h", (int(32767 * 0.7 * x / peak * min(1.0, (len(buf) - i) / fade))
                            for i, x in enumerate(buf)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


D3, A3, D5, FS5, A5, D6 = 146.83, 220.0, 587.33, 739.99, 880.0, 1174.66
INTRO = [(0.0, D3, 2.4, 0.35), (0.0, D5, 1.6, 0.5), (0.16, FS5, 1.5, 0.45),
         (0.32, A5, 1.4, 0.45), (0.48, A3, 2.0, 0.25), (0.48, D6, 1.8, 0.5)]
OUTRO = [(0.0, A5, 1.2, 0.4), (0.16, FS5, 1.2, 0.4), (0.32, D3, 2.2, 0.35), (0.32, D5, 2.0, 0.5)]


# ---------------------------------------------------------------- speech
def load_turns(path):
    """Episode file -> [(host, text)]. .json = dialogue, .txt = legacy monologue."""
    path = pathlib.Path(path)
    if path.suffix == ".json":
        import json
        turns = json.loads(path.read_text(encoding="utf-8"))["turns"]
        return [(t["speaker"] if t["speaker"] in HOSTS else HOSTS[0], t["text"]) for t in turns]
    return [(HOSTS[1], path.read_text(encoding="utf-8"))]


def _ssml(turns):
    body = []
    for host, text in turns:
        spoken = EN_TAG.sub(lambda m: f'<lang xml:lang="en-US">{m.group(1)}</lang>', escape(text))
        body.append(f'<voice name="{AZURE_VOICES[host]}">{spoken}<break time="300ms"/></voice>')
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="de-DE">' + "".join(body) + "</speak>")


def _concat(parts, out_wav):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p}'\n")
        listing = f.name
    ffmpeg("-f", "concat", "-safe", "0", "-i", listing, "-ar", str(SR), "-ac", "1", str(out_wav))
    os.unlink(listing)


def azure_speech(turns, out_wav, key, region):
    chunks, cur, size = [], [], 0
    for host, text in turns:  # stay well below Azure's per-request limits
        if cur and (len(cur) >= 30 or size + len(text) > 4500):
            chunks.append(cur)
            cur, size = [], 0
        cur.append((host, text))
        size += len(text)
    if cur:
        chunks.append(cur)
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    parts = []
    for n, chunk in enumerate(chunks):
        req = urllib.request.Request(url, data=_ssml(chunk).encode("utf-8"), method="POST", headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "riff-44100hz-16bit-mono-pcm",
            "User-Agent": "it-grc-radar"})
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    audio = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 4:
                    time.sleep(15 * (attempt + 1))
                    continue
                raise RuntimeError(f"Azure-Sprachsynthese fehlgeschlagen ({e.code}): "
                                   f"{e.read()[:300].decode('utf-8', 'replace')}")
        part = out_wav.with_name(f"{out_wav.stem}.part{n}.wav")
        part.write_bytes(audio)
        parts.append(part)
    _concat(parts, out_wav)
    for p in parts:
        p.unlink()


def piper_speech(turns, out_wav, voice):
    text = "\n\n".join(EN_TAG.sub(r"\1", t) for _, t in turns)
    subprocess.run(["piper", "-m", voice, "-f", str(out_wav), "--sentence-silence", "0.35"],
                   input=text, text=True, check=True)


# ---------------------------------------------------------------- episode
def render_episode(episode_file, out_mp3, piper_voice):
    """Speech (Azure if configured, else Piper) + intro/outro jingle, loudness-normalised MP3."""
    out_mp3 = pathlib.Path(out_mp3)
    work = out_mp3.parent
    speech, intro, outro = (work / f"{out_mp3.stem}.{n}.wav" for n in ("speech", "intro", "outro"))
    turns = load_turns(episode_file)
    key, region = os.environ.get("AZURE_SPEECH_KEY"), os.environ.get("AZURE_SPEECH_REGION")
    if key and region:
        print("Sprachsynthese: Azure (zwei Stimmen)")
        azure_speech(turns, speech, key, region.strip().lower())
        engine = "Azure"
    else:
        print("Sprachsynthese: Piper (Rückfallebene, keine Azure-Secrets gesetzt)")
        piper_speech(turns, speech, piper_voice)
        engine = "Piper"
    jingle(intro, INTRO, 3.2)
    jingle(outro, OUTRO, 2.8)
    graph = ("[0]aresample=44100,aformat=channel_layouts=mono[a0];"
             "[1]aresample=44100,aformat=channel_layouts=mono[a1];"
             "[2]aresample=44100,aformat=channel_layouts=mono[a2];"
             "[a0][a1]acrossfade=d=0.8:c1=tri:c2=tri[x];"
             "[x][a2]acrossfade=d=0.4:c1=tri:c2=tri,loudnorm=I=-16:TP=-1.5[out]")
    ffmpeg("-i", str(intro), "-i", str(speech), "-i", str(outro), "-filter_complex", graph,
           "-map", "[out]", "-ac", "1", "-ar", "44100", "-b:a", "96k", "-id3v2_version", "3",
           "-metadata", f"title=IT-GRC Radar {out_mp3.stem}", "-metadata", "artist=IT-GRC Radar",
           str(out_mp3))
    for f in (speech, intro, outro):
        f.unlink()
    return engine
