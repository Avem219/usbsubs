USB SUBTITLE  -  offline English subtitles for the movies on this USB drive
==========================================================================

WHAT IT DOES
  Copy a movie into the MOVIES folder (sub-folders are fine). On any Windows 10/11,
  Linux or macOS computer, start USB SUBTITLE. It creates  Movie.srt  next to  Movie.mkv.
  Plug the USB into your TV and play. Original videos are never modified.

  The USB does not run the AI. The computer you plug it into does the work; the USB keeps your
  movies, subtitles, settings and history (USB_SUBTITLE/config, /database, /logs).

LAYOUT
  USB/
    MOVIES/                      your films and series (Series/Episode_01.mkv ...)
    USB_SUBTITLE/
      app/                       the program (Python)
      config/settings.json       settings (portable, relative paths only)
      database/history.db        processing history (SQLite)
      logs/application.log
      temp/                      only used if temp_location = "usb"
      start_windows.bat | start_linux.sh | start_macos.command
      README.txt  TROUBLESHOOTING.txt  PACKAGING.txt

FIRST-TIME SETUP ON EACH COMPUTER (internet needed ONCE per computer)
  1. Install Python 3.9+ if missing.
       Windows: https://www.python.org/downloads/windows/  (tick "Add python.exe to PATH")
       macOS:   https://www.python.org/downloads/macos/  or  brew install python
       Linux:   sudo apt install python3 python3-venv python3-pip   (or your distro's equivalent)
  2. Double-click / run the launcher for your system. On first run it creates an environment
     on THE COMPUTER (not the USB) and installs faster-whisper + a bundled FFmpeg.
  3. The Whisper model (tiny, ~75 MB) is downloaded on first use and cached on the computer.
     Pre-download explicitly with:   <launcher> download-model
     After that the program works fully offline on that computer.
  Administrator rights are not required.

FFMPEG SETUP
  Nothing to do normally: the launcher installs the 'imageio-ffmpeg' package which bundles FFmpeg.
  If you prefer your own FFmpeg, put it on PATH (it is used first):
    Windows: winget install Gyan.FFmpeg      macOS: brew install ffmpeg      Linux: sudo apt install ffmpeg

AI MODEL SETUP
  Default: "tiny" (multilingual, fastest, lowest accuracy). Better accuracy on a stronger PC:
      type  model base   (or small)  in the program, or edit "model" in config/settings.json.
  Models are cached on the computer (~/.cache/huggingface or %USERPROFILE%\.cache\huggingface).
  Portable-model mode: set "model_location": "portable" and models are stored in USB_SUBTITLE/models
  (tiny ~75 MB, base ~145 MB, small ~480 MB). Then run download-model once on any computer with
  internet; every other computer can then work with no internet and no download.
  Strict offline: set "allow_model_download": false.

DAILY USE
  1. Plug in the USB, run the launcher. You will see the drive, movie folder and a queue.
  2. Copy movies into MOVIES. Files are processed only after their size has stopped changing for
     20 seconds and they can be opened, one movie at a time.
  3. When 'OK <movie> -> subtitle saved' appears, the .srt is beside the movie.
  4. Type  quit  and wait for "You can now eject" before unplugging.
  Commands:  status  retry  cancel  model <tiny|base|small>  lang en  folder <name>  threads <n>  logs  quit
  Background mode:  <launcher> run --no-interactive
  Process everything once and exit:  <launcher> scan
  Check this computer:  <launcher> check
  Managed copy (copies a file to MOVIES AND subtitles it at the same time):
      <launcher> copy "D:\Downloads\Film.mkv" [--subfolder Series]

HOW ENGLISH SUBTITLES ARE MADE
  Whisper detects the spoken language (votes over several 30-second samples), then:
    - English speech  -> Whisper "transcribe"
    - other languages -> Whisper's built-in "translate" task (speech -> English text directly)
  This was chosen over transcribe-then-translate because it needs no second model and keeps Whisper's
  timestamps. Quality caveat: the tiny model's translation is rough, especially for names, slang and
  less common languages. 'base' or 'small' is noticeably better. Whisper can only translate TO English,
  so other output languages are not offered in v1 (future work: optional separate translation model).

SAFE BEHAVIOUR
  - A valid English .srt that already exists is never overwritten. If a non-English or broken Movie.srt
    exists, the new file is written as Movie.en.srt (set overwrite_existing=true to replace instead).
    Delete a subtitle and it is regenerated on the next scan.
  - SRT files are written atomically (temp file, then rename) and validated before the job is marked done.
  - Unplugging the USB pauses everything; unfinished jobs return to 'pending' and resume on reconnect or
    next start. Temporary audio lives on the computer by default and is deleted after each movie.

TV INSTRUCTIONS
  - Files are plain UTF-8 SRT named exactly like the video (Film.mkv + Film.srt), no styling.
  - Play the movie, open the TV's Options/Subtitle menu, turn Subtitles on and choose the external
    file / language track. Menu names vary by brand (Samsung: Subtitle > Settings; LG: Subtitle;
    Sony/Philips: Subtitle language/track).
  - If garbled characters appear, set the TV subtitle encoding/language to UTF-8 (or Unicode), or set
    "utf8_bom": true in settings.json and delete + regenerate the .srt.
  - TV support varies: some TVs ignore .srt for certain containers or need Latin-style names without
    unusual symbols. Check your TV manual.

FILESYSTEM (FAT32 / exFAT / NTFS)
  - FAT32 cannot store a single file of 4 GB or more; many full-quality films exceed that. The program
    warns when the USB is FAT.
  - exFAT has no 4 GB limit, but not every TV can read it - check your TV's manual (supported USB formats)
    BEFORE reformatting. The program never formats or changes the drive. Reformatting erases everything.

LIMITS (honest notes)
  - Speed depends on the computer. As a rough guide tiny/int8 on a modern 4-core laptop runs several times
    faster than real time; a slow dual-core can be near real time. Test with a short clip first.
  - Whisper tiny makes mistakes and may skip quiet dialogue or mishear names. Songs and noise can create
    odd lines.
  - Managed-copy simultaneous processing works because the SOURCE file is complete and local. Subtitling an
    externally-copied, still-growing MP4/MKV is deliberately NOT attempted (MP4 often keeps its index at the
    end, so it cannot be decoded reliably until the copy finishes).
