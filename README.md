# WLC High Bay Clean Room Particle Monitor

Continuous airborne particle monitoring system initiallyfor the Wright Lab DUNE High Bay clean room (Yale University). 

[Shu] Now (since Jun 25, 2026) the module was modified to adapt to the environment in IU CRP Factory.

Data are acquired from a **Particles Plus Model 7301** optical particle counter via Modbus TCP, logged to CSV, and published as a live web dashboard on GitHub Pages after every sample cycle.

**Live dashboard:** https://rohit-raut.github.io/WLC-High-Bay-Monitoring/

[Shu] **Updated live dashboard:** https://szhang17phys.github.io/IU-CRP-Monitoring/

---

## 🚀 Quick Start (New Installations)

**Just cloned/forked this repo? Run the setup script:**

```bash
chmod +x setup.sh
./setup.sh
```

The script will:
- ✅ Check Python version
- ✅ Install dependencies (pymodbus, pyyaml)
- ✅ Configure your particle counter IP
- ✅ Test counter connectivity
- ✅ Create config.local.yaml
- ✅ Set up data directories
- ✅ Verify installation

**Then start monitoring:**
```bash
python3 particle_plus.py --all
```

[Shu] If the module is installed and operated in conda env, do
```bash
python particle_plus.py --all
```


**That's it!** See [Setup and Installation](#setup-and-installation) below for manual setup or troubleshooting.

---

## Scientific Background

Particle contamination control is a critical requirement for the construction and assembly of DUNE detector components. Airborne particulate matter can deposit onto sensitive detector surfaces during fabrication, potentially degrading performance. This system provides continuous, automated monitoring of the High Bay clean room environment to verify compliance with the specified ISO 14644-1 cleanliness class and to generate a persistent, time-stamped record of environmental conditions throughout detector assembly activities.

The Particles Plus Model 7301 measures airborne particles using **laser light scattering (optical particle counting, OPC)**. Particles drawn through the instrument at a calibrated flow rate scatter light from a 785 nm diode laser; individual scattering pulses are sized and counted. The instrument reports differential counts (counts in that size bin per sample), volumetric concentrations (counts per m³ and per ft³), and estimated PM mass concentrations (µg/m³) computed from particle size and an assumed spherical particle geometry at standard density.

### ISO 14644-1 Classification

Airborne cleanliness class is evaluated automatically from the most recent sample using the ISO 14644-1:2015 particle concentration limits. The dashboard reports the worst-case ISO class across all monitored size channels. Color coding follows the convention below:

| ISO Class | Typical Use | Badge Color |
|-----------|-------------|-------------|
| ISO 3 or 4 | Semiconductor / pharmaceutical critical zones | Green |
| ISO 5 or 6 | General semiconductor assembly | Light green |
| ISO 7 | Electronic assembly, medical devices | Yellow |
| ISO 8 | General manufacturing | Orange |
| ISO 9 | Room air (near ambient) | Red |

---

## Hardware

| Item | Value |
|------|-------|
| Instrument | Particles Plus Model 7301 |
| Measurement principle | Laser light scattering (OPC) |
| Laser | 785 nm diode |
| Flow rate | 0.1 CFM (2.83 L/min) |
| Sample duration | 60 seconds (configurable) |
| Sample interval | 30 minutes (configurable) |
| Communication | Modbus TCP (IEEE 802.3) |
| Counter IP | 10.66.66.68 |
| Modbus port | 502 |
| Host | noether cluster (rraut@noether) |

### Particle Size Channels

The instrument is configured with six simultaneous size channels covering the range relevant to ISO 14644-1 classification:

| Channel | Lower size threshold |
|---------|---------------------|
| ch1 | 0.3 µm |
| ch2 | 0.5 µm |
| ch3 | 1.0 µm |
| ch4 | 2.5 µm |
| ch5 | 5.0 µm |
| ch6 | 10.0 µm |

Each channel reports differential counts, volumetric concentration (counts/m³), and cumulative PM mass (µg/m³). Note that PM mass for the smallest channels (0.3 µm) will be at or below the instrument's floating-point precision floor and is expected to read zero; larger channels (5.0 µm, 10.0 µm) show meaningful mass concentrations.

---

## System Architecture

```
Particles Plus 7301
        |
  Modbus TCP (port 502)
        |
   noether (Linux host)
        |
   particle_plus.py
    ├── Triggers and monitors 1-min sample cycles
    ├── Syncs all stored records to measurements.csv
    ├── Syncs counter RTC to host system time (NTP-traceable)
    ├── Generates self-contained index.html dashboard
    ├── Streams live in-progress readings to live.csv (10 s interval)
    └── git commit + push to GitHub (main branch)
              |
        GitHub Pages
              |
       Live dashboard (public URL)
```

The system runs as a persistent background process (tmux session). After each completed sample cycle, it syncs all counter records, regenerates the dashboard, and pushes to GitHub. If the counter is unreachable, the dashboard is still pushed with an OFFLINE banner and the last recorded data, and the process retries automatically every 30 minutes.

---

## Modbus Register Map (Key Registers)

The Particles Plus 7301 communicates over Modbus TCP. All multi-register values use word-swapped little-endian encoding (low word in the lower register address). String registers store one ASCII character per register in the low byte of each 16-bit word.

| Register | Type | Description |
|----------|------|-------------|
| 5000 | U16 | Sampler state (0=Stopped, 1=Running, 2=Holding) |
| 5001 | U16 | Device status bits (bit 0=flow err, bit 1=laser err, bit 2=RTC not running) |
| 8004 | U16 | Erase command (write 0x9559 to erase all stored records) |
| 9001 | I32 | Total record count |
| 9002 | STR (11 regs) | Record date (YYYY-MM-DD), stored in latch buffer |
| 9013 | STR (9 regs) | Record time (HH:MM:SS), stored in latch buffer |
| 9074 | FLOAT | Sample duration (seconds) |
| 9076 | FLOAT | Flow rate (CFM) |
| 9078 | U16 | Sample status bits (bit 0=laser OK, bit 1=flow OK, bit 2=temp OK, bit 3=RH OK, bit 7=timestamp invalid) |
| 9079 | U16 | Temperature (raw x 0.1 = degrees C; 998+ = sensor absent) |
| 9080 | U16 | Relative humidity (%) |
| 1000 | STR (16 regs) | Admin password (write before any Protected Write register) |
| 1016 | STR (11 regs) | RTC date (Protected Write, requires password at reg 1000 first) |
| 1027 | STR (9 regs) | RTC time (Protected Write, requires password at reg 1000 first) |
| 10100 + 2i | FLOAT | Channel i+1 particle size threshold (µm) |
| 10300 + 2i | FLOAT | Channel i+1 differential count (counts/sample) |
| 10500 + 2i | FLOAT | Channel i+1 differential concentration (counts/ft³) |
| 10700 + 2i | FLOAT | Channel i+1 differential concentration (counts/m³) |
| 10900 + 2i | FLOAT | Channel i+1 differential PM mass (µg/m³) |
| 11500 + 2i | FLOAT | Channel i+1 cumulative concentration (counts/m³) |
| 11700 + 2i | FLOAT | Channel i+1 cumulative PM mass concentration (µg/m³) |

Records must be latched into the read buffer (register 9000) before reading. The system latches each record by index, reads all fields, then advances to the next record.

---

## Data Schema

All measurements are appended to `data/measurements.csv`. Each row corresponds to one completed 1-minute sample.

| Column | Description |
|--------|-------------|
| `record_number` | Sequential record index from instrument memory |
| `date` | Sample date from counter RTC (YYYY-MM-DD) |
| `time` | Sample time from counter RTC (HH:MM:SS) |
| `location` | Location label string stored on instrument |
| `sample_duration_s` | Actual sample duration in seconds |
| `flow_CFM` | Measured flow rate (CFM) |
| `laser_ok` | Laser status flag (True/False) |
| `flow_ok` | Flow sensor status flag |
| `temp_ok` | Temperature sensor status flag |
| `rh_ok` | Humidity sensor status flag |
| `timestamp_valid` | Counter timestamp validity flag |
| `temp_C` | Temperature (degrees C) from internal sensor |
| `RH_pct` | Relative humidity (%) |
| `ch{1-6}_size_um` | Particle size threshold for channel N (µm) |
| `ch{1-6}_diff_counts` | Differential particle count (counts/sample) |
| `ch{1-6}_diff_ft3` | Differential concentration (counts/ft³) |
| `ch{1-6}_diff_m3` | Differential concentration (counts/m³), used for ISO classification |
| `ch{1-6}_diff_mass_ugm3` | Differential PM mass concentration (µg/m³) |
| `ch{1-6}_sum_m3` | Cumulative concentration (counts/m³) |
| `ch{1-6}_pm_ugm3` | Cumulative PM mass concentration (µg/m³) |
| `sync_time` | ISO 8601 timestamp of when the Pi read this record from the instrument |

---

## Dashboard Features

The live dashboard is a self-contained static HTML page generated by `particle_plus.py` and served via GitHub Pages. It is rebuilt and pushed after every completed sample cycle (approximately every 30 minutes).

- **Status strip:** instrument connectivity, flow rate, record count, last sample time, and per-sensor status flags (laser, flow, temperature, RH)
- **Summary cards:** latest temperature (degF), relative humidity (%), and flow rate (CFM)
- **ISO 14644-1 badge:** real-time clean room classification computed from the most recent sample, color-coded by class
- **Particle counts over time:** time-series step plot of differential counts per sample across all 6 size channels (log scale). Gaps in sampling are represented as step-hold lines at the last recorded value, indicating periods of inactivity without fabricating data.
- **PM mass concentration over time:** time-series plot of cumulative PM mass (µg/m³) per channel
- **Latest size distribution:** bar chart of counts by particle size bin for the most recent sample
- **Temperature and humidity over time:** time-series from the live CSV (10-second resolution), distinct from the per-sample historical record
- **Time range selector:** adjustable from last 30 minutes to all 7 days; default is last 24 hours
- **Offline banner:** displayed when the counter is unreachable, showing the last known data with a clear warning

All timestamps on the dashboard use the counter's internal RTC, synchronized to the host system clock (NTP-traceable) at each sync cycle. The dashboard is rendered client-side using Plotly.js; no server is required.

---

## Counter Clock Synchronization

The instrument's real-time clock is synchronized to the host system time at the start of every sync cycle. Registers 1016 (date) and 1027 (time) are Protected Write registers: the admin password must first be written to register 1000, after which the date and time can be written. The system logs the pre-write and post-write clock values and reports a warning if the readback does not match the written value, which typically indicates a password mismatch.

The default configuration assumes no admin password is set (`COUNTER_PASSWORD = ''`). If a password was configured on the instrument, it must be set in `particle_plus.py`.

---

## Setup and Installation

### Requirements

- Python 3.8 or newer
- `pymodbus >= 3.5`
- Counter reachable at its configured IP address on port 502
- **Optional:** `pyyaml >= 6.0` (for config file support)
- **Optional:** Git with push access (only if you want GitHub Pages auto-push)

```bash
pip install -r requirements.txt
# Or minimal: pip install pymodbus>=3.5
```

**Important:** By default, the system runs in **monitoring-only mode** (no GitHub push). This is safe for new installations. See [GITHUB_PUSH_SETUP.md](GITHUB_PUSH_SETUP.md) to enable GitHub Pages integration.

### Deployment on noether

```bash
# 1. Clone the repository
cd /home/rraut/particle_plus
git clone git@github.com:Rohit-Raut/WLC-High-Bay-Monitoring.git .

# 2. Install dependency
pip install pymodbus>=3.5

# 3. Start in a persistent tmux session
tmux new -s particle
python3 particle_plus.py --all
# Ctrl+B, D  to detach and leave running
```

### Adapting to a Different Instrument or Location

**Recommended:** Use configuration files (easier, cleaner):

1. Copy the example config:
   ```bash
   cp config.yaml config.local.yaml
   ```

2. Edit `config.local.yaml` with your settings:
   ```yaml
   counter:
     ip: '10.66.66.68'      # Your counter's IP
     port: 502
   
   github:
     enabled: false         # Set true only if you want GitHub Pages push
   ```

3. Run the system:
   ```bash
   python3 particle_plus.py --all
   ```

**Alternative:** Edit `particle_plus.py` directly (lines 26-95), but config files are preferred for replicability.

---

## Usage

```
python3 particle_plus.py --all         Full system: sampling, syncing, live stream,
                                       and dashboard push. Recommended for tmux.

python3 particle_plus.py --sample      24/7 sampling scheduler only. Triggers counter,
                                       waits for completion, syncs, pushes dashboard.

python3 particle_plus.py --sync        One-shot: pull all new records from counter to CSV
                                       and sync the counter clock.

python3 particle_plus.py --live        Stream live in-progress readings to live.csv
                                       every 10 seconds (runs concurrently under --all).

python3 particle_plus.py --dashboard   Regenerate index.html and push to GitHub Pages.

python3 particle_plus.py --stop        Gracefully stop a running --all instance.
                                       Sends SIGTERM; current sample cycle completes first.
```

---

## Repository Layout

```
WLC-High-Bay-Monitoring/
├── particle_plus.py          Main logger, Modbus interface, and dashboard generator
├── flush_and_erase.py        Standalone utility: sync all records then optionally erase
├── test.py                   Quick Modbus connectivity and register read test
├── requirements.txt          Python dependencies
├── index.html                Auto-generated dashboard (served by GitHub Pages)
└── data/
    ├── measurements.csv      Historical sample records (appended after each sync)
    └── live.csv              Live readings at 10-second resolution (last 24 hr)
```

Runtime files created on the host (not tracked by git):

- `sync_log.txt`: timestamped log of all operations and errors
- `particle_plus.pid`: PID file used by `--stop` to locate the running process
- `data/session_baseline.txt`: record count at session start, used to exclude pre-existing records from the current session display

---

## Notes on Data Integrity

- The system only appends records with a `record_number` higher than the highest already saved, preventing duplicates across sync cycles.
- If the counter is powered off and restarted, the session baseline file ensures that records from previous sessions are not mixed into the current session's display.
- The counter's internal memory stores a fixed number of records. The `ERASE_AFTER_SYNC` flag (default `False`) can be enabled after verifying that data has been committed to CSV to free instrument memory.
- All Modbus float values use word-swapped IEEE 754 single precision. String registers use one ASCII character per 16-bit register in the low byte, which differs from the two-character-per-register encoding described in some Modbus documentation. This system implements the correct low-byte encoding, verified empirically against the instrument.

---

## Contact

Rohit Raut, Wright Lab, Yale University
DUNE Detector R&D Group
