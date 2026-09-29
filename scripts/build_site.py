#!/usr/bin/env python3
"""Build the GitHub Pages site: copy page + data, synthesize missing podcast
episodes with Piper, write the podcast RSS feed and cover image.

Runs in GitHub Actions. Episodes already published are downloaded from the
live site instead of being re-synthesized, so audio never enters git history.
"""
import datetime as dt
import email.utils
import json
import os
import pathlib
import shutil
import subprocess
import urllib.request
from xml.sax.saxutils import escape

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"
SITE_URL = os.environ.get("SITE_URL", "https://mrnemorian.github.io/ki-radar").rstrip("/")
VOICE = os.environ.get("PIPER_VOICE", str(ROOT / "voices" / "de_DE-thorsten-high.onnx"))
KEEP = 14


def sh(*args):
    subprocess.run(args, check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip() or 0)


def fetch_existing(date, target):
    try:
        with urllib.request.urlopen(f"{SITE_URL}/audio/{date}.mp3", timeout=30) as r:
            if r.status == 200:
                target.write_bytes(r.read())
                return target.stat().st_size > 10_000
    except Exception:
        pass
    return False


def synthesize(text_file, target):
    wav = target.with_suffix(".wav")
    with open(text_file, encoding="utf-8") as f:
        subprocess.run(["piper", "-m", VOICE, "-f", str(wav), "--sentence-silence", "0.35"],
                       stdin=f, check=True)
    sh("ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-af", "loudnorm=I=-16:TP=-1.5",
       "-ac", "1", "-ar", "44100", "-b:a", "64k", "-id3v2_version", "3",
       "-metadata", "title=IT-GRC Radar " + target.stem, "-metadata", "artist=IT-GRC Radar",
       str(target))
    wav.unlink()


def cover(path):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 1400), (16, 22, 20))
    d = ImageDraw.Draw(img)
    for r, c in ((520, (23, 51, 48)), (360, (14, 107, 99)), (200, (92, 198, 187))):
        d.ellipse([700 - r, 560 - r, 700 + r, 560 + r], outline=c, width=18)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 150)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 62)
    except OSError:
        font = small = ImageFont.load_default()
    d.text((700, 1150), "IT-GRC Radar", font=font, fill=(231, 238, 236), anchor="mm")
    d.text((700, 1290), "täglich · KI-generiert", font=small, fill=(154, 170, 166), anchor="mm")
    img.save(path, "PNG")


def rss(episodes):
    items = []
    for e in episodes:
        items.append(f"""  <item>
   <title>{escape(e['title'])}</title>
   <description>{escape(e['description'])}</description>
   <pubDate>{e['pubdate']}</pubDate>
   <guid isPermaLink="false">it-grc-radar-{e['date']}</guid>
   <enclosure url="{SITE_URL}/audio/{e['date']}.mp3" length="{e['size']}" type="audio/mpeg"/>
   <itunes:duration>{int(e['seconds'])}</itunes:duration>
   <itunes:explicit>false</itunes:explicit>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
 <channel>
  <title>IT-GRC Radar</title>
  <link>{SITE_URL}/</link>
  <atom:link href="{SITE_URL}/podcast.xml" rel="self" type="application/rss+xml"/>
  <language>de-de</language>
  <description>Täglich 3–5 Minuten IT-Governance, IT-Compliance und AI-Governance – automatisch recherchiert und gesprochen mit KI (Claude und Piper). Keine Rechtsberatung; im Zweifel die Quellen auf der Webseite lesen.</description>
  <itunes:author>IT-GRC Radar</itunes:author>
  <itunes:summary>Täglich 3–5 Minuten IT-Governance, IT-Compliance und AI-Governance, KI-generiert.</itunes:summary>
  <itunes:image href="{SITE_URL}/cover.png"/>
  <itunes:category text="Technology"/>
  <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
 </channel>
</rss>
"""


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "audio").mkdir(parents=True)
    for name in ("index.html", "data.json"):
        shutil.copy(ROOT / name, OUT / name)
    cover(OUT / "cover.png")

    data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
    headlines = {b["date"]: b for b in data.get("briefings", [])}
    scripts = sorted((ROOT / "episodes").glob("*.txt"), reverse=True)[:KEEP]
    episodes = []
    for txt in scripts:
        date = txt.stem
        mp3 = OUT / "audio" / f"{date}.mp3"
        if not fetch_existing(date, mp3):
            print(f"Synthetisiere Folge {date}")
            synthesize(txt, mp3)
        else:
            print(f"Folge {date} vom Live-Stand übernommen")
        b = headlines.get(date, {})
        day = dt.date.fromisoformat(date)
        episodes.append({
            "date": date,
            "title": f"{day.strftime('%d.%m.%Y')}: {b.get('headline', 'IT-GRC Radar')}"[:180],
            "description": " · ".join(b.get("points", [])) or "Tägliches IT-GRC Radar",
            "pubdate": email.utils.format_datetime(dt.datetime(day.year, day.month, day.day, 7, 15,
                                                               tzinfo=dt.timezone.utc)),
            "size": mp3.stat().st_size,
            "seconds": duration(mp3),
        })
    (OUT / "podcast.xml").write_text(rss(episodes), encoding="utf-8")
    (OUT / "episodes.json").write_text(json.dumps(
        [{"date": e["date"], "title": e["title"], "seconds": int(e["seconds"]),
          "url": f"audio/{e['date']}.mp3"} for e in episodes], ensure_ascii=False), encoding="utf-8")
    print(f"{len(episodes)} Folgen im Feed")


if __name__ == "__main__":
    main()
