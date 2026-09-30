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
# Speaking rate per host (SSML prosody), tuned by listener feedback.
AZURE_RATES = {"Seraphina": os.environ.get("AZURE_RATE_A", "+20%"),
               "Florian": os.environ.get("AZURE_RATE_B", "+10%")}
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
def load_chapters(path):
    """Episode file -> [{"title", "news_id", "turns": [(host, text)]}].
    Supports chapters (.json with "chapters"), flat dialogue (.json with "turns") and legacy .txt."""
    import json
    path = pathlib.Path(path)
    if path.suffix != ".json":
        return [{"title": "Folge", "news_id": None, "turns": [(HOSTS[1], path.read_text(encoding="utf-8"))]}]
    doc = json.loads(path.read_text(encoding="utf-8"))
    raw = doc.get("chapters") or [{"title": "Folge", "news_id": None, "turns": doc.get("turns", [])}]
    return [{"title": c.get("title") or "Kapitel", "news_id": c.get("news_id"),
             "turns": [(t["speaker"] if t["speaker"] in HOSTS else HOSTS[0], t["text"]) for t in c["turns"]]}
            for c in raw if c.get("turns")]


def wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


BRAND = re.compile(r"IT-GRC(?: Radar)?")


def _mark_brand(text):
    """Speak 'IT-GRC Radar' fully in English, with IT-GRC spelled letter by letter."""
    out, pos = [], 0
    for m in EN_TAG.finditer(text):  # leave already-marked English untouched
        out.append(BRAND.sub(lambda b: f"[en]{b.group(0)}[/en]", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(BRAND.sub(lambda b: f"[en]{b.group(0)}[/en]", text[pos:]))
    return "".join(out)


def _english(fragment, rate):
    spoken = escape(fragment).replace(
        "IT-GRC", '<say-as interpret-as="characters">ITGRC</say-as>')
    return f'<lang xml:lang="en-US"><prosody rate="{rate}">{spoken}</prosody></lang>'


def _ssml(turns):
    """One <voice> per turn; English terms become <lang> children of <voice>, and every
    text segment carries its own <prosody> so no element is nested inside <prosody>."""
    body = []
    for host, text in turns:
        rate, segments, pos = AZURE_RATES[host], [], 0
        text = _mark_brand(text)
        for m in EN_TAG.finditer(text):
            if m.start() > pos:
                segments.append(f'<prosody rate="{rate}">{escape(text[pos:m.start()])}</prosody>')
            segments.append(_english(m.group(1), rate))
            pos = m.end()
        if pos < len(text):
            segments.append(f'<prosody rate="{rate}">{escape(text[pos:])}</prosody>')
        body.append(f'<voice name="{AZURE_VOICES[host]}">{"".join(segments)}<break time="300ms"/></voice>')
    return ('<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="de-DE">' + "".join(body) + "</speak>")


def _concat(parts, out_wav):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{pathlib.Path(p).resolve().as_posix()}'\n")
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
def _speak(turns, out_wav, piper_voice, engine_state):
    key, region = os.environ.get("AZURE_SPEECH_KEY"), os.environ.get("AZURE_SPEECH_REGION")
    if key and region and engine_state.get("engine") != "Piper":
        try:
            azure_speech(turns, out_wav, key, region.strip().lower())
            engine_state["engine"] = "Azure"
            return
        except Exception as e:  # keep the podcast alive, but make the failure visible
            print(f"::warning::Azure-Sprachsynthese fehlgeschlagen, nutze Piper: {e}")
    piper_speech(turns, out_wav, piper_voice)
    wav = out_wav.with_suffix(".tmp.wav")  # normalise Piper output to the common sample rate
    ffmpeg("-i", str(out_wav), "-ar", str(SR), "-ac", "1", str(wav))
    wav.replace(out_wav)
    engine_state["engine"] = "Piper"


def render_episode(episode_file, out_mp3, piper_voice):
    """Speak each chapter (Azure if configured, else Piper), join them with intro/outro jingle,
    write a loudness-normalised MP3 with ID3 chapter marks. Returns (engine, chapters)."""
    out_mp3 = pathlib.Path(out_mp3)
    work = out_mp3.parent
    stem = out_mp3.stem
    intro, outro, speech = (work / f"{stem}.{n}.wav" for n in ("intro", "outro", "speech"))
    chapters = load_chapters(episode_file)
    state = {}
    print("Sprachsynthese: " + ("Azure (zwei Stimmen)" if os.environ.get("AZURE_SPEECH_KEY") else "Piper"))
    parts, marks = [], []
    lead_in = 3.2 - 0.8  # intro jingle length minus crossfade: where speech starts
    t = lead_in
    for i, ch in enumerate(chapters):
        part = work / f"{stem}.ch{i}.wav"
        _speak(ch["turns"], part, piper_voice, state)
        marks.append({"startTime": 0.0 if i == 0 else round(t, 2), "title": ch["title"], "news_id": ch["news_id"]})
        t += wav_seconds(part)
        parts.append(part)
    _concat(parts, speech)
    jingle(intro, INTRO, 3.2)
    jingle(outro, OUTRO, 2.8)
    total = t + 2.8 - 0.4
    meta = work / f"{stem}.chapters.txt"
    lines = [";FFMETADATA1", f"title=IT-GRC Radar {stem}", "artist=IT-GRC Radar"]
    for i, m in enumerate(marks):
        end = marks[i + 1]["startTime"] if i + 1 < len(marks) else total
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(m['startTime'] * 1000)}",
                  f"END={int(end * 1000)}", "title=" + m["title"].replace("=", "-").replace(";", ",")]
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
    graph = ("[0]aresample=44100,aformat=channel_layouts=mono[a0];"
             "[1]aresample=44100,aformat=channel_layouts=mono[a1];"
             "[2]aresample=44100,aformat=channel_layouts=mono[a2];"
             "[a0][a1]acrossfade=d=0.8:c1=tri:c2=tri[x];"
             "[x][a2]acrossfade=d=0.4:c1=tri:c2=tri,loudnorm=I=-16:TP=-1.5[out]")
    ffmpeg("-i", str(intro), "-i", str(speech), "-i", str(outro), "-f", "ffmetadata", "-i", str(meta),
           "-filter_complex", graph, "-map", "[out]", "-map_metadata", "3", "-map_chapters", "3",
           "-ac", "1", "-ar", "44100", "-b:a", "96k", "-id3v2_version", "3", str(out_mp3))
    for f in parts + [speech, intro, outro, meta]:
        f.unlink()
    return state.get("engine", "Piper"), marks
