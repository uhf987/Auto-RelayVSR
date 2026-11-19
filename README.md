# RelayVSR Batch Upscaler

A small desktop GUI that automates long-video upscaling with
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) and the RelayVSR x4 workflow.

Long videos run out of VRAM when processed in one go. This tool splits the video into
short chunks, feeds them to a running ComfyUI instance one at a time, and merges the
results back into a single file. If the job is interrupted (power cut, crash, closing
the app), it resumes where it left off.

## Features

- Pick a video (or drag & drop it) - no manual splitting or merging
- Chunks are created in a work folder next to your video
- Progress bar with percentage and estimated time left
- **Resume support**: progress is saved in `state.json` after every step
- Detects ComfyUI failures (e.g. out-of-memory) and shows a short error message
- Works with any API-format workflow that has a `LoadVideo` and a `SaveVideo` node

## Requirements

- Windows (the code is plain Python and should also work on Linux/macOS)
- Python 3.8+ (not needed if you use the prebuilt `.exe`)
- [ffmpeg](https://ffmpeg.org/download.html) in your `PATH`, **or** `ffmpeg.exe` placed next to the program
- A running ComfyUI with the RelayVSR custom nodes and models installed
- `pip install requests` (and optionally `tkinterdnd2` for drag & drop)

## Usage

1. Start ComfyUI.
2. Run `python relayvsr_batch_gui.py` (or the `.exe`).
3. Fill in:
   - **Video** - the file to upscale
   - **ComfyUI address** - usually `http://127.0.0.1:8188`
   - **Workflow (API .json)** - your workflow exported with *Export (API)*
   - **Chunk length** - seconds per chunk (see the VRAM note below)
4. Press **Start / Resume**.

To resume an interrupted job, start ComfyUI, select the **same video** and press
**Start / Resume** again. Finished chunks are skipped.

### Output

Next to your video, `<name>_work/` is created:

```
<name>_work/
  parts/        split chunks
  output/       upscaled chunks downloaded from ComfyUI
  state.json    progress (used for resuming)
<name>_upscaled.mp4   final merged video
```

Delete the `_work` folder to start a job from scratch. The intermediate files in
`parts/` and `output/` (and the uploaded copies in ComfyUI's `input/videos/`) are not removed
automatically.

## Workflow requirements

- It must be saved in **API format** (ComfyUI menu: *Export (API)*).
- It must contain one `LoadVideo` node and one `SaveVideo` node. The tool sets the
  input file and the output filename prefix of these nodes for every chunk.

## VRAM notes

Out-of-memory errors are almost always caused by the **input resolution** and the
**number of frames per chunk**. The model upscales x4, so a 1080p input needs far more
memory than an 8 GB GPU has. Tips:

- Downscale the source first (e.g. 720x576 / 854x480 worked on an RTX 3060 Ti, 8 GB)
- Use shorter chunks (2-5 s), especially for 50/60 fps videos
- Restart ComfyUI and close other GPU-heavy apps before a long run

## Notes

- Splitting uses `-c copy` (no re-encoding), so cuts happen at keyframes and chunk
  lengths are approximate.
- Progress inside a chunk is an estimate based on the previous chunks' duration.

## Building the exe

Put `utility_relayvsr_video_x4_upscale.json` next to the script and run `build_exe.bat`.
The result is `dist\RelayVSR_Batch_Upscaler.exe`. The workflow is embedded in the exe;
a copy placed next to the exe takes priority. If antivirus software flags the one-file
build, replace `--onefile` with `--onedir` in `build_exe.bat`.
