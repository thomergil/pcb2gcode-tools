#!/usr/bin/env python3
"""
pcb2gcode-combine

Combines multiple G-code files (drill, milldrill, outline) into a single file.
Only safe when all files use the same tool/bit size.

Ensures safe Z-height transitions between operations to prevent scratching
or breaking the bit.
"""

import argparse
import os
import re
import sys
from pygcode import Line, GCodeRapidMove, GCodeLinearMove, GCodeSpindleSpeed, \
    GCodeFeedRate, GCodeStartSpindleCW, GCodeStopSpindle

# Z height detection threshold (mm) - only used to identify tool change retracts
# which signal end of header section. Actual safe_z is extracted from source files.
TOOL_CHANGE_HEIGHT_MIN = 30.0  # Z >= this indicates tool change retract

# Parser states
STATE_HEADER = 'header'
STATE_TOOL_CHANGE = 'tool_change'
STATE_OPERATIONS = 'operations'
STATE_FOOTER = 'footer'

# G-code comment templates
COMMENT_SECTION = "( === Operations from {} === )"
COMMENT_RETRACT_BEFORE = "( retract before next operation set )"
COMMENT_RETRACT_AFTER = "( retract after operations )"
COMMENT_SAFETY_RETRACT = "( safety retract )"
COMMENT_SPINDLE_SPEED = "( spindle speed for {} )"
COMMENT_FEEDRATE = "( feedrate for {} )"

# Tool size patterns in comments
TOOL_SIZE_PATTERNS = [
    r'drill size\s+([0-9.]+)\s*mm',
    r'cutter diameter\s+([0-9.]+)\s*mm',
    r'mill head of\s+([0-9.]+)\s*mm',
    r'Bit sizes:\s*\[([0-9.]+)mm\]',
]


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
            - tool_size: float in mm or None
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
    saw_m3 = False
    tool_size = None

    for i, raw_line in enumerate(raw_lines):
        parsed = Line(raw_line)

        # Extract tool size from any comment
        if tool_size is None:
            tool_size = extract_tool_size(parsed)

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
        'tool_size': tool_size,
    }


def combine_gcode_files(input_files, output_file):
    """
    Combine multiple G-code files into one.

    Uses header and tool change from first file.
    Adds operations from all files with safe Z transitions.
    Uses footer from last file.
    """
    if len(input_files) < 2:
        print("Error: Need at least 2 files to combine.", file=sys.stderr)
        return False

    parsed_files = []
    for filepath in input_files:
        if not os.path.exists(filepath):
            print(f"Error: File not found: {filepath}", file=sys.stderr)
            return False
        parsed = parse_gcode_file(filepath)
        parsed_files.append(parsed)
        tool_str = f"{parsed['tool_size']}mm" if parsed['tool_size'] else "unknown"
        print(f"Parsed {os.path.basename(filepath)}: "
              f"{len(parsed['operations'])} ops, "
              f"S{parsed['spindle_speed']}, F{parsed['feedrate']}, "
              f"tool={tool_str}")

    # Validate tool sizes match
    tool_sizes = [p['tool_size'] for p in parsed_files]
    known_sizes = [s for s in tool_sizes if s is not None]

    if known_sizes:
        unique_sizes = set(known_sizes)
        if len(unique_sizes) > 1:
            print(f"\nERROR: Tool sizes don't match!", file=sys.stderr)
            for p in parsed_files:
                size = p['tool_size']
                print(f"  {os.path.basename(p['filepath'])}: {size}mm" if size
                      else f"  {os.path.basename(p['filepath'])}: unknown", file=sys.stderr)
            print("\nCombining files with different tool sizes is dangerous!", file=sys.stderr)
            return False
        print(f"Tool size: {known_sizes[0]}mm (verified across all files)")
    else:
        print("Warning: Could not determine tool sizes from any file")

    # Use safe Z height from first file (must be present)
    safe_z = parsed_files[0]['safe_z']
    if safe_z is None:
        print("ERROR: Could not determine safe Z height from first file!", file=sys.stderr)
        print("The file format may not be supported.", file=sys.stderr)
        return False
    print(f"Safe Z height: {safe_z}mm (from {os.path.basename(parsed_files[0]['filepath'])})")

    output_lines = []

    # Add header from first file
    output_lines.extend(parsed_files[0]['header'])

    # Add tool change from first file
    output_lines.extend(parsed_files[0]['tool_change'])

    # Add operations from each file with safe transitions
    for i, parsed in enumerate(parsed_files):
        basename = os.path.basename(parsed['filepath'])

        # Add comment indicating which file's operations follow
        output_lines.append(f"\n{COMMENT_SECTION.format(basename)}\n")

        # If not the first file, ensure we're at safe height before starting
        if i > 0:
            output_lines.append(f"G00 Z{safe_z:.5f} {COMMENT_RETRACT_BEFORE}\n")

        # Set spindle speed if different from previous file
        current_speed = parsed['spindle_speed']
        prev_speed = parsed_files[i - 1]['spindle_speed'] if i > 0 else current_speed
        if current_speed and current_speed != prev_speed:
            output_lines.append(f"S{current_speed} {COMMENT_SPINDLE_SPEED.format(basename)}\n")

        # Set feedrate if different from previous file
        current_feedrate = parsed['feedrate']
        prev_feedrate = parsed_files[i - 1]['feedrate'] if i > 0 else current_feedrate
        if current_feedrate and i > 0 and current_feedrate != prev_feedrate:
            output_lines.append(f"G01 F{current_feedrate:.5f} {COMMENT_FEEDRATE.format(basename)}\n")

        # Add operations
        ops = parsed['operations']
        if ops:
            # Check if operations start with a safe positioning move
            first_parsed = Line(ops[0])
            z = get_z_from_line(first_parsed)
            if not is_rapid_move(first_parsed) or z is None:
                output_lines.append(f"G00 Z{safe_z:.5f} {COMMENT_SAFETY_RETRACT}\n")

            output_lines.extend(ops)

            # Ensure we end at safe height before next file
            last_parsed = Line(ops[-1])
            z = get_z_from_line(last_parsed)
            if z is None or z < 0:
                output_lines.append(f"G00 Z{safe_z:.5f} {COMMENT_RETRACT_AFTER}\n")

    # Add footer from last file
    output_lines.extend(parsed_files[-1]['footer'])

    # Write output
    with open(output_file, 'w') as f:
        f.writelines(output_lines)

    total_ops = sum(len(p['operations']) for p in parsed_files)
    print(f"\nCombined {len(input_files)} files into {output_file}")
    print(f"Total operation lines: {total_ops}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="""
        Combine multiple pcb2gcode G-code files into a single file.

        Use this when drill, milldrill, and outline all use the same bit size.
        The script ensures safe Z-height transitions between operations.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("input_files", nargs='+',
                        help="Input G-code files to combine (in order)")
    parser.add_argument("-o", "--output", required=True,
                        help="Output combined G-code file")

    args = parser.parse_args()

    if len(args.input_files) < 2:
        print("Error: Need at least 2 input files to combine.", file=sys.stderr)
        sys.exit(1)

    success = combine_gcode_files(args.input_files, args.output)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
