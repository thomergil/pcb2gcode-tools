#!/usr/bin/env python3
"""
Shared G-code parsing and validation utilities for pcb2gcode-tools.

This module provides common functionality for combine.py and multitool.py:
- G-code file parsing
- Safety validation (units, dangerous commands, spindle speeds)
- Helper functions for Z height detection, spindle control, etc.
"""

import os
import re
from pygcode import Line, GCodeRapidMove, GCodeLinearMove, GCodeSpindleSpeed, \
    GCodeFeedRate, GCodeStartSpindleCW, GCodeStopSpindle

# Re-export pygcode classes for convenience
__all__ = [
    'Line', 'GCodeSpindleSpeed',
    # ... other exports added below
]

# =============================================================================
# Constants
# =============================================================================

# Z height detection threshold (mm) - only used to identify tool change retracts
# which signal end of header section. Actual safe_z is extracted from source files.
TOOL_CHANGE_HEIGHT_MIN = 30.0  # Z >= this indicates tool change retract

# Default values
DEFAULT_TOOL_CHANGE_Z = 35.0  # mm
DEFAULT_SPINDLE_DWELL = 3.0   # seconds
DEFAULT_SAFE_Z = 1.0          # mm - conservative default if not detected

# Validation thresholds
MIN_SPINDLE_SPEED = 100       # RPM - anything below is suspicious
MAX_SPINDLE_SPEED = 30000     # RPM - anything above is suspicious
MIN_SAFE_Z = 0.5              # mm - safe Z must be above this
MAX_SAFE_Z_DIFFERENCE = 10.0  # mm - warn if safe_z differs by more than this between files

# Units
UNITS_MM = 'mm'
UNITS_INCHES = 'inches'

# Dangerous G-codes that should not appear in normal PCB milling files
DANGEROUS_GCODES = {
    'G28': 'Home command - may crash into workpiece',
    'G30': 'Secondary home - may crash into workpiece',
}

# Parser states
STATE_HEADER = 'header'
STATE_TOOL_CHANGE = 'tool_change'
STATE_OPERATIONS = 'operations'
STATE_FOOTER = 'footer'

# Tool size patterns in comments
TOOL_SIZE_PATTERNS = [
    r'drill size\s+([0-9.]+)\s*mm',
    r'cutter diameter\s+([0-9.]+)\s*mm',
    r'mill head of\s+([0-9.]+)\s*mm',
    r'Bit sizes:\s*\[([0-9.]+)mm\]',
]

# Tool type patterns - used to describe the tool in MSG comments
TOOL_TYPE_PATTERNS = [
    (r'drill', 'drill'),
    (r'milldrill', 'milldrill'),
    (r'outline', 'outline cutter'),
    (r'back', 'isolation mill'),
    (r'front', 'isolation mill'),
]

# G-code comment templates
COMMENT_SECTION = "( === Operations from {} === )"
COMMENT_RETRACT_BEFORE = "( retract before next operation set )"
COMMENT_RETRACT_AFTER = "( retract after operations )"
COMMENT_SAFETY_RETRACT = "( safety retract )"
COMMENT_DWELL_SYNC = "( dwell to ensure Z complete before XY )"
COMMENT_SPINDLE_SPEED = "( spindle speed for {} )"
COMMENT_FEEDRATE = "( feedrate for {} )"

# =============================================================================
# Helper Functions
# =============================================================================


def is_tool_change_height(z):
    """Check if Z is at tool change height."""
    return z is not None and z >= TOOL_CHANGE_HEIGHT_MIN


def is_safe_height(z):
    """Check if Z is positive but below tool change height (working safe height)."""
    return z is not None and 0 < z < TOOL_CHANGE_HEIGHT_MIN


def get_z_from_line(line):
    """Extract Z value from a parsed line, if present."""
    for gc in line.gcodes:
        if isinstance(gc, (GCodeRapidMove, GCodeLinearMove)):
            if gc.Z is not None:
                return gc.Z
    return None


def get_spindle_speed(line):
    """Extract spindle speed from a parsed line, if present."""
    for gc in line.gcodes:
        if isinstance(gc, GCodeSpindleSpeed):
            return int(gc.word.value)
    return None


def get_feedrate(line):
    """Extract feedrate from a parsed line, if present."""
    for gc in line.gcodes:
        if isinstance(gc, GCodeFeedRate):
            return float(gc.word.value)
    return None


def has_spindle_on(line):
    """Check if line has M3 (spindle on clockwise)."""
    for gc in line.gcodes:
        if isinstance(gc, GCodeStartSpindleCW):
            return True
    return False


def has_spindle_off(line):
    """Check if line has M5 (spindle stop)."""
    for gc in line.gcodes:
        if isinstance(gc, GCodeStopSpindle):
            return True
    return False


def is_rapid_move(line):
    """Check if line is a G0 rapid move."""
    for gc in line.gcodes:
        if isinstance(gc, GCodeRapidMove):
            return True
    return False


def extract_tool_size(parsed_line):
    """Extract tool/bit size from a parsed G-code line's comment."""
    if not parsed_line.comment:
        return None

    comment_text = parsed_line.comment.text
    for pattern in TOOL_SIZE_PATTERNS:
        m = re.search(pattern, comment_text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def infer_tool_type(filepath):
    """Infer tool type from filename."""
    basename = os.path.basename(filepath).lower()
    for pattern, tool_type in TOOL_TYPE_PATTERNS:
        if re.search(pattern, basename):
            return tool_type
    return 'tool'


# =============================================================================
# G-code File Parsing
# =============================================================================


def parse_gcode_file(filepath):
    """
    Parse a G-code file into sections using pygcode.

    Returns:
        dict with keys:
            - header: list of raw lines
            - tool_change: list of raw lines
            - operations: list of raw lines
            - footer: list of raw lines
            - spindle_speed: int or None
            - feedrate: float or None
            - safe_z: float (working safe height)
            - tool_change_z: float (high Z for tool changes)
            - tool_size: float in mm or None
            - tool_type: str (inferred from filename)
            - units: 'mm' or 'inches' or None
            - dangerous_commands: list of (line_num, code, reason) tuples
    """
    with open(filepath, 'r') as f:
        raw_lines = f.readlines()

    header = []
    tool_change = []
    operations = []
    footer = []

    state = STATE_HEADER
    spindle_speed = None
    feedrate = None
    safe_z = None  # Must be extracted from file
    tool_change_z = None  # Tool change height (e.g., Z35)
    saw_m3 = False
    tool_size = None
    units = None  # 'mm' or 'inches'
    dangerous_commands = []  # List of dangerous commands found

    for i, raw_line in enumerate(raw_lines):
        parsed = Line(raw_line)

        # Extract tool size from any comment
        if tool_size is None:
            tool_size = extract_tool_size(parsed)

        # Detect units (G20=inches, G21=mm)
        for gc in parsed.gcodes:
            if hasattr(gc, 'word'):
                if gc.word.letter == 'G' and gc.word.value == 20:
                    units = UNITS_INCHES
                elif gc.word.letter == 'G' and gc.word.value == 21:
                    units = UNITS_MM

        # Detect dangerous commands
        line_upper = raw_line.upper().strip()
        for dangerous_code, reason in DANGEROUS_GCODES.items():
            if dangerous_code in line_upper:
                dangerous_commands.append((i + 1, dangerous_code, reason))

        if state == STATE_HEADER:
            # Extract spindle speed from header
            s = get_spindle_speed(parsed)
            if s is not None:
                spindle_speed = s

            # Extract feedrate
            f = get_feedrate(parsed)
            if f is not None:
                feedrate = f

            # Header ends when we see retract to tool change height
            z = get_z_from_line(parsed)
            if is_tool_change_height(z):
                tool_change_z = z
                state = STATE_TOOL_CHANGE
                tool_change.append(raw_line)
                continue

            # For millready files (no tool change), M3 signals end of header
            if has_spindle_on(parsed):
                state = STATE_TOOL_CHANGE
                saw_m3 = True  # M3 was just seen
                tool_change.append(raw_line)
                continue

            header.append(raw_line)

        elif state == STATE_TOOL_CHANGE:
            # Extract spindle speed if not found in header (pcb2gcode often puts S on M3 line)
            if spindle_speed is None:
                s = get_spindle_speed(parsed)
                if s is not None:
                    spindle_speed = s

            # Track M3 (spindle on)
            if has_spindle_on(parsed):
                saw_m3 = True
                tool_change.append(raw_line)
                continue

            # After M3, everything goes to operations (including G04 dwell)
            if saw_m3:
                state = STATE_OPERATIONS
                operations.append(raw_line)
                # Extract safe_z if this is a safe Z move
                z = get_z_from_line(parsed)
                if z is not None and is_safe_height(z):
                    safe_z = z
                continue

            tool_change.append(raw_line)

        elif state == STATE_OPERATIONS:
            # Extract safe_z from first positive Z rapid move if not yet found
            if safe_z is None:
                z = get_z_from_line(parsed)
                if z is not None and is_safe_height(z):
                    safe_z = z

            # Extract feedrate from operations if not found in header
            if feedrate is None:
                f = get_feedrate(parsed)
                if f is not None:
                    feedrate = f

            # Footer starts at "All done" comment or final high Z retract before M5
            if parsed.comment and 'All done' in parsed.comment.text:
                state = STATE_FOOTER
                footer.append(raw_line)
                continue

            # Check for final retract (high Z followed soon by M5)
            z = get_z_from_line(parsed)
            if is_tool_change_height(z):
                # Look ahead for M5
                for j in range(i + 1, min(i + 5, len(raw_lines))):
                    future_parsed = Line(raw_lines[j])
                    if has_spindle_off(future_parsed):
                        state = STATE_FOOTER
                        footer.append(raw_line)
                        break
                if state == STATE_FOOTER:
                    continue

            operations.append(raw_line)

        elif state == STATE_FOOTER:
            footer.append(raw_line)

    return {
        'header': header,
        'tool_change': tool_change,
        'operations': operations,
        'footer': footer,
        'filepath': filepath,
        'spindle_speed': spindle_speed,
        'feedrate': feedrate,
        'safe_z': safe_z,
        'tool_change_z': tool_change_z,
        'tool_size': tool_size,
        'tool_type': infer_tool_type(filepath),
        'units': units,
        'dangerous_commands': dangerous_commands,
    }


# =============================================================================
# Validation
# =============================================================================


def validate_files_for_combining(parsed_files, require_same_tool=False):
    """
    Validate that files can be safely combined.

    Args:
        parsed_files: List of parsed file dicts from parse_gcode_file()
        require_same_tool: If True, require all files use same tool size

    Returns (is_valid, errors, warnings) where:
        - is_valid: False if files should NOT be combined (dangerous)
        - errors: List of error messages (fatal)
        - warnings: List of warning messages (non-fatal)
    """
    errors = []
    warnings = []

    # Check for unit consistency
    units_found = {}
    for p in parsed_files:
        name = os.path.basename(p['filepath'])
        if p['units']:
            units_found[name] = p['units']

    unique_units = set(units_found.values())
    if len(unique_units) > 1:
        errors.append("FATAL: Unit mismatch detected - cannot combine files with different units!")
        for name, unit in units_found.items():
            errors.append(f"  {name}: {unit}")
        errors.append("This would result in dangerous incorrect movements.")

    # Check for inches (typically a mistake for PCB work)
    if UNITS_INCHES in units_found.values():
        warnings.append("WARNING: One or more files use inches (G20) instead of mm (G21)")
        warnings.append("This is unusual for PCB milling - verify this is intentional.")

    # Check for missing spindle speed
    for p in parsed_files:
        name = os.path.basename(p['filepath'])
        if p['spindle_speed'] is None:
            errors.append(f"FATAL: {name} has no spindle speed - cannot determine safe RPM")
        elif p['spindle_speed'] < MIN_SPINDLE_SPEED:
            warnings.append(f"WARNING: {name} has very low spindle speed S{p['spindle_speed']}")
        elif p['spindle_speed'] > MAX_SPINDLE_SPEED:
            warnings.append(f"WARNING: {name} has very high spindle speed S{p['spindle_speed']}")

    # Check for dangerous commands
    for p in parsed_files:
        name = os.path.basename(p['filepath'])
        for line_num, code, reason in p['dangerous_commands']:
            errors.append(f"FATAL: {name} line {line_num}: {code} - {reason}")

    # Check for safe_z consistency
    safe_z_values = [(os.path.basename(p['filepath']), p['safe_z'])
                     for p in parsed_files if p['safe_z'] is not None]
    if safe_z_values:
        z_values = [z for _, z in safe_z_values]
        z_range = max(z_values) - min(z_values)
        if z_range > MAX_SAFE_Z_DIFFERENCE:
            warnings.append(f"WARNING: Safe Z heights vary significantly ({z_range:.1f}mm range):")
            for name, z in safe_z_values:
                warnings.append(f"  {name}: Z{z}")
            warnings.append("This may indicate files from different setups.")

    # Check for missing safe_z
    missing_safe_z = [os.path.basename(p['filepath'])
                      for p in parsed_files if p['safe_z'] is None]
    if missing_safe_z:
        warnings.append(f"WARNING: Could not detect safe Z height in: {', '.join(missing_safe_z)}")
        warnings.append(f"Will use conservative default of Z{DEFAULT_SAFE_Z}")

    # Check tool sizes match if required
    if require_same_tool:
        tool_sizes = [p['tool_size'] for p in parsed_files]
        known_sizes = [s for s in tool_sizes if s is not None]

        if known_sizes:
            unique_sizes = set(known_sizes)
            if len(unique_sizes) > 1:
                errors.append("FATAL: Tool sizes don't match!")
                for p in parsed_files:
                    size = p['tool_size']
                    name = os.path.basename(p['filepath'])
                    if size:
                        errors.append(f"  {name}: {size}mm")
                    else:
                        errors.append(f"  {name}: unknown")
                errors.append("Combining files with different tool sizes requires --multi mode (pcb2gcode-multitool).")
        else:
            warnings.append("Warning: Could not determine tool sizes from any file")

    is_valid = len(errors) == 0
    return is_valid, errors, warnings


def get_safe_z_from_files(parsed_files):
    """
    Extract safe Z height from parsed files.

    Returns the safe_z from the first file that has one,
    or DEFAULT_SAFE_Z if none found.
    """
    for p in parsed_files:
        if p['safe_z'] is not None:
            return p['safe_z']
    return DEFAULT_SAFE_Z


def get_tool_change_z_from_files(parsed_files):
    """
    Extract tool change Z height from parsed files.

    Returns the tool_change_z from the first file that has one,
    or DEFAULT_TOOL_CHANGE_Z if none found.
    """
    for p in parsed_files:
        if p['tool_change_z'] is not None:
            return p['tool_change_z']
    return DEFAULT_TOOL_CHANGE_Z


def generate_state_header():
    """
    Generate explicit state initialization header for defense in depth.

    Returns list of G-code lines that establish a known machine state.
    """
    return [
        "G90        ( Absolute distance mode )\n",
        "G21        ( Units: mm )\n",
        "G17        ( XY plane selection )\n",
        "G94        ( Feed rate: units per minute )\n",
        "\n",
    ]


def filter_header_redundant_commands(header_lines, filter_spindle_speed=True):
    """
    Filter out redundant state commands from header that we set explicitly.

    Args:
        header_lines: List of header lines to filter
        filter_spindle_speed: If True, remove S commands (for multi-tool mode where
                              we set S per tool). If False, keep S commands (for
                              same-tool mode where header S applies to all operations).

    Returns filtered list of header lines.
    """
    state_commands = {'G90', 'G91', 'G20', 'G21', 'G17', 'G18', 'G19', 'G93', 'G94'}
    filtered = []

    for line in header_lines:
        parsed_line = Line(line)

        # Skip lines with spindle speed if filtering is enabled (multi-tool mode)
        if filter_spindle_speed:
            has_spindle_speed = any(isinstance(gc, GCodeSpindleSpeed) for gc in parsed_line.gcodes)
            if has_spindle_speed:
                continue

        # Check if line contains only state commands we already set
        gcode_words = [gc.word.letter + str(int(gc.word.value))
                       for gc in parsed_line.gcodes
                       if hasattr(gc, 'word') and gc.word.letter == 'G']
        is_redundant_state = all(word in state_commands for word in gcode_words) and gcode_words
        if is_redundant_state:
            continue

        filtered.append(line)

    return filtered


def strip_leading_dwells(operations):
    """
    Strip leading G04/G4 dwell commands from operations.

    Tool change sequences generate their own dwell, so leading dwells
    from the original file are redundant.

    Returns operations list with leading dwells removed.
    """
    start_idx = 0
    for idx, line in enumerate(operations):
        stripped = line.strip().upper()
        # Check for G4 or G04 at start of line (dwell command)
        if stripped.startswith('G4 ') or stripped.startswith('G04') or stripped.startswith('G4P'):
            start_idx = idx + 1
        else:
            break
    return operations[start_idx:]
