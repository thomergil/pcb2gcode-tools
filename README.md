# pcb2gcode-tools

Post-processing tools for [pcb2gcode](https://github.com/pcb2gcode/pcb2gcode) G-code output, optimized (including safety) for CNC machines like the Carbide 3D Nomad 3 with [OpenCNCPilot](https://github.com/martin2250/OpenCNCPilot). 

## Installation

```bash
brew install pipx    # if not already installed
pipx install git+https://github.com/thomergil/pcb2gcode-tools
```

This installs three commands:
- `pcb2gcode-wrapper` - Runs `pcb2gcode` with automatic offset calculation
- `pcb2gcode-fixup` - Post-processes G-code for compatibility and safety
- `pcb2gcode-combine` - Combines multiple G-code files into one

## Quick Start

```bash
# From your KiCad export directory containing .gbr and .drl files:
pcb2gcode-wrapper myboard --mill-diameters=0.169

# Output:
#   myboard_00_back.ngc      - back copper traces
#   myboard_01_drill.ngc     - drill + milldrill + outline (combined)
```

## Tools

### pcb2gcode-wrapper

Orchestrates the full workflow:
1. Runs `pcb2gcode` three times (with `--back`, and `--drill`, and `--outline`)
2. Auto-calculates x-offset from `Edge_Cuts.gbr` board dimensions
3. Runs `pcb2gcode-fixup` on each output file
4. Runs `pcb2gcode-combine` to merge drill/milldrill/outline

```bash
pcb2gcode-wrapper BASENAME [options] [pcb2gcode options...]

Options:
  --x-margin MM      X margin for offset calculation (default: 5)
  --y-margin MM      Y margin for y-offset (default: 3)
  --no-combine       Skip combining drill/milldrill/outline
  --output-dir DIR   Output directory for generated files

# Examples:
pcb2gcode-wrapper myboard --mill-diameters=0.169
pcb2gcode-wrapper myboard --output-dir ./output --x-margin 10
pcb2gcode-wrapper myboard --no-combine  # keep files separate
```

### pcb2gcode-fixup

Post-processes G-code for CNC compatibility and safety:
- Filters unsupported commands (G64, G94)
- Removes M6 tool change sequences (`--remove-m6`)
- Swaps initial Z/XY moves for safety (moves to XY position before plunging)

```bash
pcb2gcode-fixup input.ngc output.ngc [options]

Options:
  --remove-m6              Remove M6 tool change sequences
  --min-segment-length MM  Remove tiny Voronoi artifacts (default: 0)
```

### pcb2gcode-combine

Combines multiple G-code files into one when they use the same bit:

```bash
pcb2gcode-combine drill.ngc milldrill.ngc outline.ngc -o combined.ngc
```

Features:
- Validates tool sizes match across all files
- Safe Z-height transitions between operations
- Preserves spindle speed changes with proper dwell times
- Extracts safe Z height from source files

## `millproject` configuration

Create a `millproject` file in your Gerber directory for `pcb2gcode`:

```ini
metric=true
metricoutput=true

# milling
zwork=-0.06
zsafe=20
zchange=35
mill-feed=100
mill-speed=12000
nom6=1
spinup-time=3.0
spindown-time=3.0
isolation-width=0.6

# Voronoi mode (optional, leaves more copper for easier soldering)
voronoi=1

# drilling
zdrill=-1.7
zmilldrill=-1.7
drill-feed=100
drill-speed=12000
nog81=1
drills-available=1.0
min-milldrill-hole-diameter=1.01
milldrill-diameter=1.0

# outline
zcut=-1.7
cut-feed=100
cut-speed=16000
cutter-diameter=1.0
cut-infeed=0.6
bridgesnum=0
```

Key settings:
- `nom6=1` - Prevents M6 commands that trip up some controllers
- `nog81=1` - Uses G0/G1 instead of canned drill cycles
- `zsafe` - Travel height; start high (20mm), lower once confident
- `zwork` - Milling depth; start shallow (e.g., -0.05), adjust as needed

## Requirements

- Python 3.8+
- [pcb2gcode](https://github.com/pcb2gcode/pcb2gcode) installed and in PATH

## Development

```bash
git clone https://github.com/thomergil/pcb2gcode-tools
cd pcb2gcode-tools
pipx install -e .  # editable install for development
```

## License

MIT
