# YouTube Shorts Auto-Clipper ✂️

Kasih link video YouTube → otomatis dipotong jadi klip vertikal **9:16** (< 60 detik)
dengan **caption kata-per-kata** yang dibakar ke video, siap upload sebagai Shorts.

Give it a YouTube video URL → it downloads the video and automatically cuts it
into vertical **9:16** Shorts clips (< 60s) with **word-by-word captions**
burned in, ready to upload.

---

## Cara kerja / How it works

1. **Download** — `yt-dlp`, video+audio terbaik (mp4, maks. 1080p).
2. **Transkrip** — `faster-whisper` (CPU-friendly, model default `base`),
   dengan timestamp per kata. / Transcription with word timestamps.
3. **Segmentasi** — klip 30–59 detik dipilih dari jeda percakapan (gap),
   lalu **diranking berdasarkan skor potensi viral** (lihat di bawah),
   tidak tumpang tindih, maksimal N klip. Kalau tidak ada suara,
   fallback ke potongan waktu merata.
   Clips are picked from speech gaps, then ranked by viral-potential score.
4. **Reframe** — center crop ke 1080x1920 vertikal.
5. **Caption** — subtitle ASS gaya karaoke (kata yang sedang diucapkan
   berubah kuning, sisanya putih, outline hitam, bawah-tengah) dibakar
   dengan ffmpeg. / Karaoke-style word-by-word captions burned in.
6. **Output** — `output/clip_01.mp4` dst. + `output/clips.json` (manifest:
   URL sumber, start/end, judul yang disarankan, `viral_score` 0–100,
   dan kalimat `hook`).

> Catatan: crop saat ini center crop. Smart crop yang mengikuti wajah
> (face-tracking) adalah perbaikan untuk versi berikutnya.
> Note: reframing is a center crop for now; face-tracking smart crop is a
> planned future improvement.

---

## Clipping stack / Tumpukan alat

Stack yang dipakai dan direkomendasikan untuk workflow clipping ini,
dari [postingan @RoundtableSpace](https://x.com/RoundtableSpace/status/2121630485537255559)
tentang clipping stack (semua open-source, dicek: aktif dan banyak dipakai):

| Alat | Status di tool ini | Peran |
|---|---|---|
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) — 67K ★ | ✅ **Inti / core** | Ekstrak audio, reframe 1080x1920, bakar caption ASS. Semua rendering video di tool ini lewat ffmpeg. |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | ✅ **Inti / core** (default) | Transkrip + timestamp per kata, CPU-friendly. Bukan dari postingan itu, tapi ini default `--transcriber` di sini. |
| [WhisperX](https://github.com/m-bain/whisperX) — 25K ★, BSD-2-Clause | ⚙️ **Opsional** — `--transcriber whisperx` | ASR dengan timestamp per kata + speaker diarization (siapa bicara kapan). Alternatif transcriber; output kata-katanya dikonversi ke format yang sama, jadi scoring + caption jalan seperti biasa. |
| [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) — 5,5K ★ | ⚙️ **Opsional** — `--scene-detect` | Deteksi pergantian scene; titik potong klip di-snap ke batas scene terdekat (±1,5 detik) supaya potongan tidak jatuh di tengah gerakan. |
| [auto-editor](https://github.com/WyattBlue/auto-editor) — 5,9K ★, Unlicense | 📝 **Opsional, hanya dokumentasi** | Editor video otomatis (mis. buang bagian hening). Tidak ada jalur kode di tool ini — dipakai mandiri sebagai pre-step kalau kamu download videonya sendiri dulu: `auto-editor video.mp4 --margin 0.4s` (cek `auto-editor --help` untuk opsi lain). |
| [Remotion](https://github.com/remotion-dev/remotion) — 73K ★ | 🔁 **Dipakai di stack lain** | Video programatik dengan React. Dipakai di serial stickman penulis, bukan di CLI ini (caption di sini dibakar langsung oleh ffmpeg). Disebut karena satu ekosistem: kalau butuh template/motion grafis yang lebih rumit, Remotion jalurnya. |

### WhisperX (`--transcriber whisperx`)

```bash
pip install whisperx   # lihat requirements.txt, bagian opsional
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" --transcriber whisperx
```

Kalau `whisperx` belum terinstal, tool ini **berhenti dengan pesan install
yang jelas** — tidak diam-diam balik ke faster-whisper, supaya kamu tahu
persis engine apa yang jalan.

**Diarization (`--diarize`)** — label pembicara per kata (pyannote).
Butuh token Hugging Face yang kamu set sendiri:

```bash
export HF_TOKEN="hf_..."   # token kamu sendiri, dengan akses ke model
                            # pyannote speaker-diarization (terima dulu
                            # lisensinya di huggingface.co)
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" \
    --transcriber whisperx --diarize
```

Tanpa token, tool memberi warning dan lanjut **tanpa** diarization.
Jangan pernah menulis token ke file atau ke kode.

### Scene detection (`--scene-detect`)

```bash
pip install scenedetect   # lihat requirements.txt, bagian opsional
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" --scene-detect
```

Kalau `scenedetect` belum terinstal, tool memberi warning dan lanjut
tanpa snapping (perilaku default tidak berubah). Tidak berpengaruh
dengan `--timestamps` (rentang manual dipakai apa adanya).

---

## Skor potensi viral / Viral-potential score

Setiap klip dinilai 0–100 dari sinyal-sinyal ini (dari transkrip + audio):

| Sinyal | Maks | Keterangan |
|---|---|---|
| **Hook 3 detik pertama** | 30 | Pertanyaan ("kenapa", "gimana", "apa", "why", "how") atau curiosity gap ("ternyata", "rahasia", "jangan", "secret", "stop") di awal klip |
| **Puncak energi** | 25 | Momen keras/cepat: lonjakan volume, bicara cepat, audio yang dinamis (bukan datar) |
| **Pernyataan kuat** | 20 | Hot take / klaim berani / emosi ("terbaik", "paling", "tidak pernah", "best", "never", "always"), tawa atau seruan |
| **Payoff** | 15 | Ada punchline/penutup di dalam klip ("jadi", "akhirnya", "ternyata", "that's why") atau puncak energi di sepertiga akhir |
| **Kepadatan bicara** | 10 | Seberapa penuh klip diisi ucapan |

**Pengurangan nilai:** −15 kalau tidak ada hook jelas di 3 detik pertama,
−10 kalau klip isinya filler saja ("eh", "um", "anu", "like", "you know"),
−10 kalau audio datar/monoton.

> ⚠️ **Jujur dulu: skor ini BUKAN janji viral.** Skor potensi viral
> hanyalah heuristik — alat ini menemukan momen yang *kemungkinan*
> menarik perhatian berdasarkan hook, energi, dan payoff. Apakah klip
> benar-benar viral tergantung algoritma, timing, audiens, dan
> keberuntungan — tidak ada yang di sini menjamin views.
>
> ⚠️ **Honest note: this score is NOT a promise of virality.** It is a
> heuristic that finds moments with *higher viral potential*. Whether a
> clip actually goes viral depends on the algorithm, timing, audience
> and luck — nothing here guarantees views.

---

## Instalasi / Installation

Butuh / Requirements: **Python 3.10+** dan **ffmpeg**.

### Ubuntu / Debian

```bash
sudo apt update && sudo apt install -y ffmpeg python3-venv
git clone https://github.com/<username>/youtube-shorts-auto-clipper.git
cd youtube-shorts-auto-clipper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Termux (Android)

```bash
pkg install python ffmpeg
pip install -r requirements.txt
```

---

## Penggunaan / Usage

```bash
# Otomatis: 3 klip terbaik, bahasa Indonesia
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Video berbahasa Inggris, model kecil (lebih cepat di CPU)
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" --lang en --model tiny

# 5 klip, durasi 20–45 detik, tanpa caption
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" --num-clips 5 --min-len 20 --max-len 45 --no-captions

# Manual: tentukan sendiri rentang waktunya (menggantikan segmentasi otomatis)
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" --timestamps "1:05-1:50,3:00-3:45"

# Transkrip dengan WhisperX (timestamp kata + diarization, butuh HF_TOKEN)
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" --transcriber whisperx --diarize

# Potongan dirapikan ke batas scene (PySceneDetect)
python clip.py "https://www.youtube.com/watch?v=VIDEO_ID" --scene-detect
```

| Opsi / Option | Default | Keterangan |
|---|---|---|
| `--num-clips` | `3` | Maksimal klip yang dibuat / max clips |
| `--min-len` | `30` | Durasi minimum klip (detik) |
| `--max-len` | `59` | Durasi maksimum klip (detik, Shorts ≤ 60) |
| `--lang` | `id` | Bahasa transkrip (`id`, `en`, ...) |
| `--model` | `base` | Model faster-whisper / WhisperX: `tiny`, `base`, `small`, `medium` |
| `--transcriber` | `faster-whisper` | Engine transkrip: `faster-whisper` atau `whisperx` |
| `--diarize` | — | Dengan WhisperX: label pembicara (butuh `HF_TOKEN`) |
| `--scene-detect` | — | Snap titik potong ke batas scene (PySceneDetect) |
| `--no-captions` | — | Lewati caption |
| `--timestamps` | — | Rentang manual `"1:05-1:50,3:00-3:45"` |
| `--workdir` | `downloads` | Folder video sumber |
| `--out` | `output` | Folder klip hasil |

Contoh manifest `output/clips.json`:

```json
{
  "source_url": "https://www.youtube.com/watch?v=...",
  "source_title": "Sintel | Official Full Movie",
  "scoring": "viral-potential score 0-100: a heuristic ... never guarantees views.",
  "clips": [
    {
      "file": "clip_01.mp4",
      "start_sec": 12.5,
      "end_sec": 68.0,
      "duration_sec": 55.5,
      "viral_score": 72,
      "hook": "Why did you leave me?",
      "title": "Sintel mencari naga kecilnya ..."
    }
  ]
}
```

---

## Yang penting / Important

- ⚠️ **Hanya pakai video milikmu sendiri, atau video public-domain /
  Creative Commons.** Mendownload video orang lain bisa melanggar
  Terms of Service YouTube. / Only use videos you own or that are
  public-domain / CC-licensed.
- YouTube Shorts maksimal **60 detik** — `--max-len` di atas 60 otomatis
  dipotong ke 60.
- Semua proses jalan di **CPU** (tidak butuh GPU). Model `tiny` paling
  cepat, `base` lebih akurat. / CPU-only, no GPU required.
- Kualitas crop tergantung video sumber: video 1080p memberi hasil
  terbaik untuk output 1080x1920.

## Troubleshooting / Masalah umum

**`Sign in to confirm you're not a bot` / HTTP 429 saat download**
YouTube kadang memblokir IP server/VPS. Tool ini otomatis mencoba
client Android sebagai fallback (biasanya tetap jalan, kadang kualitas
lebih rendah). Kalau tetap gagal:
- Coba lagi nanti (blokir 429 sering hilang sendiri).
- Atau download dengan cookies browser sendiri:
  `yt-dlp --cookies-from-browser chrome <url>` lalu jalankan `clip.py`
  dengan file lokal (lihat `--help`).
- Di Termux/HP (IP residensial) masalah ini jarang terjadi.

**Transkrip gagal dengan error `av.open() ... metadata_errors`**
Versi `av` (PyAV) terbaru tidak lagi cocok dengan faster-whisper.
Tool ini menghindari masalah itu dengan mengirim audio sebagai array
numpy (diekstrak dulu dengan ffmpeg), jadi tidak memakai decoder PyAV
sama sekali. Pastikan `numpy` terinstal (`pip install -r requirements.txt`).
If transcription fails with an `av.open() ... metadata_errors` error,
this tool avoids it by passing audio as a numpy array instead of a file
path — make sure `numpy` is installed.

## Lisensi / License

MIT — lihat `LICENSE`.
