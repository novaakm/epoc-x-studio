# prestEEG — User Manual

**prestEEG** streams, records, replays, and analyzes real-time EEG and motion data from **Emotiv EPOC-X** headsets (14 EEG channels `AF3`–`AF4` + 7 motion channels `MOT0`–`MOT6` over Bluetooth Low Energy or LSL) as well as **Muse** headbands (4 EEG channels `TP9`, `AF7`, `AF8`, `TP10` over Lab Streaming Layer) — with no external runtime service or cloud account required.

---

## 1. What the App Does

- **Live BLE & LSL Streaming**: Connects to configured headsets over BLE or consumes Lab Streaming Layer (LSL) streams in real time.
- **Unified Desktop Hub**: Streams live headset data or replays recorded CSV files to multiple visualization windows simultaneously over a built-in local stream broker.
- **Live & Offline Visualization**: Inspect electrode contact quality, raw waveforms, 3D cortical frequency bands, and side-by-side recording comparisons.
- **Multi-Device Web Brain Map**: Serve live 3D Brain Maps and headmaps in a browser for several EPOC-X and Muse devices simultaneously on a local network without Qt.
- **Recording & Timed Notes**: Save sessions to CSV at any point during a stream and log timestamped text annotations that appear directly on the waveform timeline.

---

## 2. What the App Consists Of

| Component | Command | Role |
|---|---|---|
| **prestEEG** | `presteeg` | Main desktop control hub, device profile manager, recorder, and local TCP stream broker (`127.0.0.1:18765`). |
| **Headmap** | `presteeg-headmap` | Live 2D vector head diagram showing per-electrode contact quality, signal quality, and Yaw/Pitch/Roll head orientation (EPOC-X & Muse layouts). |
| **Live Waveforms** | `presteeg-live-viewer` | High-speed PyQtGraph oscilloscope for stacked EEG and motion traces, synchronized replay scrubbing, and event note overlays. |
| **3D Brain Map** | `presteeg-brain-map` | 3D WebGL cortical viewer displaying real-time FFT frequency bands across 8 bilateral cortical regions (with spatially interpolated dorsal crown), rolling band-power graphs, and cognitive indices. |
| **LSL Web Server** | `presteeg-lsl-brain-server` | Headless multi-device HTTP/SSE server streaming the 3D Brain Map and live headmaps to browsers from LSL streams. |
| **CSV Compare** | `presteeg-csv-compare` | Side-by-side offline recording viewer with millisecond time-shift alignment, measurement cursors, and live Pearson *r* correlation. |
| **CLI Utilities** | `presteeg-record`, `presteeg-take-notes`, `presteeg-from-lsl`, `presteeg-save-lsl` | Standalone command-line tools for headless recording, terminal note logging, and LSL bridging. |

---

## 3. Installation & Setup

### Install

**Windows (recommended)**
Double-click `install.bat` in the repository root (it creates `venv` if needed and runs `install.ps1`), or run manually in PowerShell:
```powershell
python -m venv venv
.\install.ps1
```
`install.bat` / `install.ps1` install the package in editable mode (`pip install -e .`) and create Desktop and Start Menu shortcuts for **prestEEG**. You can also launch it via `launcher.bat`, `launcher.ps1`, or `.\venv\Scripts\presteeg.exe`.

**Linux / macOS (or manual install)**
```bash
python3 -m venv venv
./venv/bin/pip install -e .
# Optional Lab Streaming Layer (LSL) support:
./venv/bin/pip install -e ".[lsl]"
```

### Configure Device Profiles (`KEYS.txt`)

> [!IMPORTANT]
> For direct BLE streaming, each EPOC-X headset requires its own per-device **AES-256 key** and **1-byte whitening constant (`W`)**. No keys are shipped in this repository.

1. Open **prestEEG** → click **Devices...** → enter your headset's:
   - **BT Name** (e.g. `EPOC-X`) and **BT Address** (or leave `(auto)` to auto-discover)
   - **AES-256 Key** (32 or 64 hex characters)
   - **Whitening Byte** (e.g. `0x14`, or `0x55` on older firmware)
2. Click **Save / Update**. Profiles are stored locally in git-ignored `KEYS.txt` (never committed to version control).

---

## 4. How to Use prestEEG

### A. Stream a Live Headset or Replay a CSV
1. **Live Headset**: In the **Stream Source** card, select **Live Headset**, pick your saved profile (and check **LSL** if needed), then click **Connect Stream**.
2. **Replay CSV**: Switch to **Replay CSV**, click **Browse...** to pick a recorded `.csv` file (defaults to `recordings/eeg_recording.csv`), choose playback speed (`0.5x`, `1x`, `2x`, `4x`) and **Loop**, then click **Start Replay**.
   - During replay, drag or click the **progress bar** to seek — prestEEG, Live Waveforms, and the 3D Brain Map stay synchronized.
3. Click **Pause Stream** / **Resume Stream** at any time to freeze or resume all connected views.

### B. Record CSVs & Timed Event Notes
- **Record to Disk**: In **CSV Recording & Timed Notes**, choose an output path and click **Start Recording**. You can start and stop recording mid-stream without interrupting BLE or open viewers.
- **Log Event Notes**: Type an annotation in the **Note** field and press `Enter` (or **Save Note**).
  - The note's **start timestamp** is captured on your first keystroke and its **end timestamp** when you press `Enter`.
  - Notes are saved to `<recording>.notes.csv` and broadcast live onto the **Live Waveforms** timeline.

### B2. Brain Map in a Browser for LSL Streams (no Qt, many viewers)

`presteeg-lsl-brain-server` serves the 3D Brain Map as a web page for several devices at once, following their Lab Streaming Layer streams (`presteeg-record --lsl --lsl-name NAME`, or `muse-record --lsl`). It needs only `numpy` and `pylsl` (`pip install -e ".[lsl]"`), not PyQt, so it can run headless on a recording machine:

```bash
presteeg-lsl-brain-server --host 0.0.0.0 --port 8766 \
    epocx1='EPOCX (E1234567)' epocx2='EPOCX (E7654321)' muse1=Muse
```

Open `http://HOST:8766/?device=muse1`; the device menu at the top switches between the streams. Each device gets its own DSP, so viewers of different devices do not disturb each other. Streams that are not up yet are waited for.

- **Muse (4 electrodes: TP9, AF7, AF8, TP10)**: AF7/AF8 stand for both frontal regions of their side, TP9/TP10 for the temporal ones; the occipital regions have no electrode and are drawn dark ("no electrode"); the parietal crown is interpolated from the remaining regions (`MUSE_REGION_MAPPING` in `brain_map_view.py`).
- `--headmap-dir DIR`: show the live head map of the device (the image `status-image-<device>.png`, as written by an external head map feeder) in the right panel; `--logger-port PORT`: add a "Logger" link to a web app on this host.
- `--exit-with PID`: stop together with the process that started the server.
- The page can also be served by another web server under `/presteeg/` (or `/studio/`) using relative assets; it then talks to this backend on port 8766 (CORS is enabled).

### C. Using the Visualization Windows

Click **Launch All Live Views** to open Headmap, Live Waveforms, and 3D Brain Map together, or launch any module individually:

- **Headmap**
  - **Outer ring color** shows contact state: Grey (no data), Red (flat/railed/off-head), Orange (noisy/settling), Green (good contact).
  - **Inner dot size & color** (Red → Orange → Green) show clean EEG signal quality.
  - **Y / P / R bars** at the center show head Yaw, Pitch, and Roll. Click **Zero** (or click anywhere on the headmap) to re-center orientation; click **Float** (or press `F`) to keep the window always on top.
- **Live Waveforms**
  - Displays stacked panels for the 14 EEG channels (top) and 7 motion channels (bottom), plus a top banner for timed event notes.
  - **Controls**: `Space` pauses/resumes; `Ctrl + Scroll` (or trackpad pinch / `+` and `-`) zooms the time window; drag the horizontal divider between panels to resize them; **Reset View** snaps back to the live playhead.
  - **When Paused in Replay**: Scroll or press `←` / `→` to pan through history, or click and drag directly on the plot to scrub the playhead (dragging near the left/right plot edges auto-scrolls).
- **3D Brain Map**
  - **Camera**: Left-drag to orbit, scroll or use the **Zoom** slider to zoom, and adjust **Glow Bloom**, **Outline Opacity**, or **Auto-Rotation** in the right sidebar.
  - **Band Selection**:
    - Click any band pill (**All Bands**, **Alpha**, **Beta**, **Theta**, **Gamma**, **Delta**) to isolate that band on the 3D cortex and expand its live rolling time-series graph.
    - Click a band's **colored dot swatch** to enable or disable that band (disabled bands turn grey and are excluded from **All Bands** dominance).
  - **Baseline Calibration**: Click **Calibrate 5s Baseline** while resting to lock a 5-second personal baseline; click again to unlock and return to uncalibrated relative power.
  - **Hover & Fullscreen**: Hover over any cortical lobe to inspect its channel list, dominant band, activity percentage, and `α / β / θ` breakdown. Press `F` or `F11` for fullscreen (sidebars auto-hide in fullscreen or narrow windows and slide out when hovering the screen edges).
- **CSV Compare**
  - Drag and drop (or click **Browse...**) to load **File A** (blue) and **File B** (amber).
  - Filter displayed channels with checkboxes or presets (**EEG Only**, **All**, **None**).
  - **Navigate**: Scroll wheel zooms at the cursor, click-drag pans, `←` / `→` / `+` / `-` step pan and zoom, and the **Timeline** slider scrubs the recording.
  - **Inspect & Align**: Click any plot to drop a synchronized measurement cursor showing exact timestamps and values for both files. Adjust **Shift File B** (`-5000` to `+5000 ms`) to align recordings and watch each channel's Pearson correlation (`r`) update live.

---

## 5. How the Analyses Are Performed

### A. Packet Decoding & Channel Layout
- **EEG Frames**: Every 64-byte BLE packet contains two 32-byte EEG frames (`~128 Hz` sample rate). Each frame carries a 1-byte counter and 14 unsigned 16-bit ADC counts (`~0x8000` midpoint at rest) ordered left-to-right:
  `AF3, F7, F3, FC5, T7, P7, O1 | O2, P8, T8, FC6, F4, F8, AF4`.
- **Motion & Status**: 84-byte packets append one 16-byte block of 7 big-endian `int16` motion channels (`MOT0`–`MOT6`, `~32 Hz`) plus a 4-byte status/quality trailer (`status_a, quality_a, status_b, quality_b`).

### B. 3D Brain Map: Band Discrimination, Cortical Mapping & Graphs
The 3D Brain Map (`BrainDSP` in `src/presteeg/brain_map_view.py`) analyzes a sliding **1.0-second window** (`128` samples on EPOC-X, `256` samples on Muse) updated **20 times per second**:

1. **Signal Conditioning & Artifact Detection**
   - **Artifact Check**: If any channel's standard deviation across the 1-second window exceeds the configured threshold (`2500` ADC counts on EPOC-X, `1300 µV` on Muse), the frame is flagged as `EMG ARTIFACT CLAMPED`.
   - **Detrending & Common Median Reference (CMR)**: Each channel is linearly detrended (removing DC offset and slow drift), and on multi-channel EPOC-X layouts the median across all 14 channels at each time step is subtracted to reject global common-mode noise.
   - **Pre-Emphasis & FFT**: A first-order difference (`np.diff`) and Hanning window are applied before computing the real FFT power spectral density (PSD).
   - **1/f Spectral Equalization**: Raw EEG naturally follows a `1/f` power drop-off where slow Delta waves overpower faster bands. By default, `BrainDSP` shapes the spectrum with a high-frequency shelf above `15 Hz` and normalizes each band by its frequency width (with Delta's lower bound raised to `1.5 Hz` to reject eye-blink/sway drift) so all five bands compete on an even footing.

2. **Frequency Bands & Cortical Region Mapping**
   Power is integrated across five standard EEG bands and averaged into **8 bilateral cortical regions** (while the dorsal central/parietal crown, which has no electrodes over `C3/Cz/C4` or `P3/Pz/P4`, is smoothly interpolated per-vertex from surrounding regions on each hemisphere):

   | Band | Frequency Range | Color | Typical Correlate |
   |---|---|---|---|
   | **Delta** | `0.5–4 Hz` (`1.5–4 Hz` equalized) | Red (`#ef4444`) | Deep slow activity / drowsiness |
   | **Theta** | `4–8 Hz` | Yellow (`#facc15`) | Drowsiness, inward focus, memory encoding |
   | **Alpha** | `8–12 Hz` | Purple (`#a855f7`) | Relaxed wakefulness (strongest with eyes closed) |
   | **Beta** | `12–30 Hz` | Green (`#22c55e`) | Active thinking, alertness, motor/task focus |
   | **Gamma** | `30–45 Hz` | Pink (`#ec4899`) | High-frequency cognitive processing |

   | Cortical Region (Left / Right) | EPOC-X Electrodes | Muse Electrodes | Anatomical Gyri Covered |
   |---|---|---|---|
   | **Dorsal Frontal L / R** | `AF3, F3` / `AF4, F4` | `AF7` / `AF8` | Superior & middle frontal gyri |
   | **Ventral Frontal L / R** | `F7, FC5` / `F8, FC6` | `AF7` / `AF8` | Inferior frontal gyrus (orbitalis, triangularis, opercularis) |
   | **Temporal L / R** | `T7, P7` / `T8, P8` | `TP9` / `TP10` | Superior/middle/inferior temporal + posterior temporo-parietal (`T5/T6`) |
   | **Occipital L / R** | `O1` / `O2` | `--` *(no electrode)* | Occipital poles |
   | *(Interpolated)* **Parietal L / R** | `--` *(Adjacent regions)* | `--` *(Adjacent regions)* | Precentral, postcentral, supramarginal & superior parietal gyri |

3. **Band Discrimination, Baseline Calibration & Lobe Glow**
   - **Relative Share**: In each lobe, a band's power is divided by the sum of all five bands in that lobe (`0%–100%`), then smoothed over time with an Exponential Moving Average (`α = 0.25`).
   - **5s Baseline Calibration**: When locked, each lobe's band power is first divided by its 5-second resting average and weighted by waking priors (`Delta 10%, Theta 20%, Alpha 28%, Beta 28%, Gamma 14%`) before normalizing — highlighting changes relative to your own resting state.
   - **3D Rendering**: In **All Bands (Dominant)** mode, each lobe glows in the color of its highest-percentage enabled band; in single-band mode, all lobes display the selected band's color. Lobe brightness and opacity scale smoothly across a `10%–45%` relative power range.

4. **Band Graphs & Live Cognitive Indices**
   - **Expandable Band Graphs**: Each band pill expands into a rolling `~6-second` (`120`-frame) SVG chart of global average band share (auto-scaled between `50%` and `100%`). In **All Bands**, all enabled bands are plotted together with the globally dominant band highlighted by a thicker line and shaded fill.
   - **Alpha Relaxation**: Mean relative Alpha share across active posterior lobes.
   - **Engagement Index**: Ratio of global `Beta / (Alpha + Theta)` (clipped to `0.00–3.00`), reflecting active alertness relative to slower rhythms.

### C. Headmap: Contact Quality, Signal Quality & Orientation
- **Contact Ring (`1.5 s` window)**: Evaluates whether each electrode is flat/railed, measures its correlation (`≥ 0.40`) against the common-mode signal across channels, and checks signal mobility (`std(diff) / std ≤ 0.79`) with EMA smoothing and an on-head consensus vote (capped by hardware quality values when present).
- **Signal Quality Dot (`2.0 s` window)**: Linearly detrends the signal and combines three factors into a `0–1` score: healthy amplitude (`2–60 µV` SD), absence of large spike artifacts (`> 6× SD`), and a high ratio of `1–30 Hz` EEG power vs. `48–52 Hz` mains line noise.
- **Head Orientation**: Computes Roll and Pitch from accelerometer channels and tilt-compensated Yaw from magnetometer channels when available.

### D. Live Waveforms: Normalization & Event Overlay
- **Per-Channel Scaling**: By default, each trace is independently min/max normalized (`[0, 1]`) over the currently visible time window so small EEG fluctuations and larger baseline offsets remain readable side-by-side without clipping. Passing `--shared-range-groups` switches selected plot groups to a shared physical Y-axis.
- **Timed Notes**: Notes from prestEEG or `<recording>.notes.csv` are rendered as vertical dashed lines (for instant markers) or shaded time spans (when duration `≥ 100 ms`).

### E. CSV Compare: Envelope Decimation & Correlation
- **Min/Max + Mean Decimation**: To keep long recordings responsive at any zoom level, samples inside the visible window are bucketed into one time bin per screen pixel. Each pixel column draws both a translucent **min-to-max vertical envelope** (so brief spikes never disappear when zoomed out) and a solid **mean trace**.
- **Time-Shift & Live Pearson *r***: Shifting File B (`±5000 ms`) offsets its timestamps relative to File A. For every visible channel, each File A sample in the current view window is paired with the nearest shifted File B sample (within `max(1 ms, 1.5 × median sample interval)`), and the Pearson correlation coefficient **`r`** and matched sample count **`n`** are computed live (`r ≥ 0.90` highlighted green).

---

## 6. Command-Line Usage & File Formats

### CLI Entry Points
All tools can be run standalone from `venv/Scripts` (Windows) or `venv/bin` (Linux/macOS):

```bash
# Launch the full desktop hub
presteeg

# Stream EEG + motion to stdout and save to disk
presteeg-record --device EPOC-X --csv recordings/session.csv

# Pipe a live stream or connect a viewer to a running prestEEG broker
presteeg-record | presteeg-live-viewer --width=5s --group=5-18 --group=19-25
presteeg-headmap --connect 127.0.0.1:18765
presteeg-brain-map --replay recordings/eeg_recording.csv

# Serve live 3D Brain Maps in a browser for LSL streams
presteeg-lsl-brain-server --host 0.0.0.0 --port 8766 epocx1='EPOCX (E1234567)' muse1=Muse

# Compare two CSV recordings side-by-side
presteeg-csv-compare recordings/file_a.csv recordings/file_b.csv

# Log timed terminal notes alongside a recording
presteeg-take-notes recordings/session.csv
```

Key `presteeg-record` options:
- `--device NAME` / `--address ADDR` / `--name BT_NAME`: Select headset profile from `KEYS.txt` or target a specific BLE device.
- `--key HEX` / `--whiten BYTE`: Supply AES key (`$PRESTEEG_AES_KEY`) or whitening byte (`$PRESTEEG_WHITEN`) directly.
- `--csv FILE`: Save CSV to disk while also streaming to `stdout`.
- `--lsl` / `--lsl-name NAME`: Publish to Lab Streaming Layer instead of CSV (use `presteeg-from-lsl` or `presteeg-save-lsl` to consume/record LSL streams).
- `--offline CAPTURE.csv`: Decode a raw-packet CSV offline.

### CSV & Notes File Formats
- **Recording CSV (`.csv`)**:
  `timestamp_ns, timestamp_iso, packet_size, frame_counter`, 14 EEG columns (`AF3 F7 F3 FC5 T7 P7 O1 O2 P8 T8 FC6 F4 F8 AF4`), 7 motion columns (`MOT0..MOT6`), `status_a, quality_a, status_b, quality_b`, and optional `cipher_hex` (`--raw`).
- **Timed Notes (`.notes.csv`)**:
  `start_ns, start_iso, end_ns, end_iso, note`

---

## 7. Repository Structure

```text
prestEEG/
├── src/
│   ├── presteeg/        # Core EEG acquisition, stream broker, PyQt6 hub, LSL web server & viewers
│   └── epoc_studio/     # Lightweight compatibility shims for external LSL integrations
├── scripts/             # Standalone Windows .bat / .ps1 pipeline helpers
├── recordings/          # Bundled demo recording (eeg_recording.csv); new captures git-ignored
├── KEYS.txt             # Local per-device AES keys & whitening bytes (git-ignored)
├── install.bat / .ps1   # Editable venv install + Windows shortcut creator
├── launcher.bat / .ps1  # Quick launch wrappers for prestEEG
└── pyproject.toml
```

---

## 8. Authors & Acknowledgments

- **prestEEG** by **Michal Novák** and **Ondřej Bojar**.
