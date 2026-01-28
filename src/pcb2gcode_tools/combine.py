#!/usr/bin/env python3
"""
pcb2gcode-combine

Combines multiple G-code files into a single file with proper safety transitions.

Modes:
  - Default (same tool): All files must use the same tool size. Operations are
    concatenated with safe Z transitions between them.
  - Multi-tool (--multi): Files may use different tools. Proper M6 tool change
    sequences are inserted between files, prompting the operator to change bits.

Safety features:
  - Validates units consistency (refuses to combine mm with inches)
  - Detects dangerous commands (G28, G30)
  - Validates spindle speeds
  - Adds explicit state header (G90 G21 G17 G94) for defense in depth
  - Ensures safe Z transitions between all operations
"""

import argparse
import os
import sys

from .gcode_utils import (
    # Parsing
    parse_gcode_file,
    Line,
    GCodeSpindleSpeed,
    # Validation
    validate_files_for_combining,
    get_safe_z_from_files,
    get_tool_change_z_from_files,
    # Helpers
    get_z_from_line,
    is_rapid_move,
    # Constants
    DEFAULT_TOOL_CHANGE_Z,
    DEFAULT_SPINDLE_DWELL,
    COMMENT_SECTION,
    COMMENT_RETRACT_BEFORE,
    COMMENT_RETRACT_AFTER,
    COMMENT_SAFETY_RETRACT,
    COMMENT_DWELL_SYNC,
    COMMENT_SPINDLE_SPEED,
    COMMENT_FEEDRATE,
    # Defense in depth
    generate_state_header,
    filter_header_redundant_commands,
    strip_leading_dwells,
)


def generate_tool_change_sequence(tool_number, tool_size, tool_type, spindle_speed,
                                   tool_change_z=DEFAULT_TOOL_CHANGE_Z,
                                   dwell_time=DEFAULT_SPINDLE_DWELL,
                                   is_first_tool=False):
    """
    Generate a tool change sequence for multi-tool mode.

    Args:
        is_first_tool: If True, skip M6 pause (operator already loaded first tool)

    Returns list of G-code lines for tool change.
    """
    lines = []

    if is_first_tool:
        # First tool: explicit spindle speed (header S commands are filtered out)
        if spindle_speed:
            lines.append(f"G00 S{spindle_speed}     (RPM spindle speed.)\n")
        lines.append("M3      (Spindle on clockwise.)\n")
        lines.append(f"G04 P{dwell_time:.5f} (Wait for spindle to get up to speed)\n")
    else:
        # Subsequent tools: full tool change sequence
        lines.append(f"G00 Z{tool_change_z:.5f} (Retract)\n")
        lines.append(f"T{tool_number}\n")
        lines.append("M5      (Spindle stop.)\n")
        lines.append(f"G04 P{dwell_time:.5f}\n")

        # Message about tool change
        if tool_size:
            lines.append(f"(MSG, Change tool bit to {tool_type} size {tool_size}mm)\n")
        else:
            lines.append(f"(MSG, Change tool bit to {tool_type})\n")

        lines.append("M6      (Tool change.)\n")

        if spindle_speed:
            lines.append(f"G00 S{spindle_speed}     (RPM spindle speed.)\n")
        lines.append("M3      (Spindle on clockwise.)\n")

    return lines


def combine_files(input_files, output_file, multi_tool=False):
    """
    Combine multiple G-code files into one.

    Args:
        input_files: List of input file paths
        output_file: Output file path
        multi_tool: If True, allow different tools and insert M6 sequences

    Returns True on success, False on failure.
    """
    if len(input_files) < 2:
        print("Error: Need at least 2 files to combine.", file=sys.stderr)
        return False

    # Parse all files
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
              f"tool={tool_str}" +
              (f", type={parsed['tool_type']}" if multi_tool else ""))

    # Validate files
    is_valid, errors, warnings = validate_files_for_combining(
        parsed_files,
        require_same_tool=not multi_tool
    )

    # Print warnings
    for warning in warnings:
        print(warning, file=sys.stderr)

    # If validation failed, print errors and abort
    if not is_valid:
        print("\n" + "=" * 60, file=sys.stderr)
        print("CANNOT COMBINE FILES - SAFETY VALIDATION FAILED", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        for error in errors:
            print(error, file=sys.stderr)
        print("\nAborting to prevent potentially dangerous G-code.", file=sys.stderr)
        return False

    # Get safe heights
    safe_z = get_safe_z_from_files(parsed_files)
    tool_change_z = get_tool_change_z_from_files(parsed_files)
    print(f"Safe Z height: {safe_z}mm")
    print(f"Tool change Z height: {tool_change_z}mm")

    # Report spindle speed differences in multi-tool mode
    if multi_tool:
        spindle_speeds = [(os.path.basename(p['filepath']), p['spindle_speed']) for p in parsed_files]
        unique_speeds = set(s for _, s in spindle_speeds if s is not None)
        if len(unique_speeds) > 1:
            print(f"\nNote: Files have different spindle speeds:")
            for name, speed in spindle_speeds:
                print(f"  {name}: S{speed}")
            print("Each tool will use its own spindle speed.\n")
    else:
        # Same-tool mode: report tool size if known
        tool_sizes = [p['tool_size'] for p in parsed_files]
        known_sizes = [s for s in tool_sizes if s is not None]
        if known_sizes:
            print(f"Tool size: {known_sizes[0]}mm (verified across all files)")

    # Build output
    output_lines = []

    # Add explicit state header (defense in depth)
    output_lines.append(f"( pcb2gcode-combine {'multi-tool' if multi_tool else 'same-tool'} output )\n")
    output_lines.extend(generate_state_header())

    # Add filtered header from first file
    # In multi-tool mode, filter S commands (we set them per tool)
    # In same-tool mode, keep S commands (header S applies to all operations)
    header = filter_header_redundant_commands(parsed_files[0]['header'], filter_spindle_speed=multi_tool)
    output_lines.extend(header)

    if multi_tool:
        # Multi-tool mode: insert tool change sequences
        for i, parsed in enumerate(parsed_files):
            basename = os.path.basename(parsed['filepath'])
            tool_number = i + 1

            # Generate tool change sequence
            tc_lines = generate_tool_change_sequence(
                tool_number=tool_number,
                tool_size=parsed['tool_size'],
                tool_type=parsed['tool_type'],
                spindle_speed=parsed['spindle_speed'],
                tool_change_z=tool_change_z,
                is_first_tool=(i == 0),
            )

            output_lines.append(f"\n( === Tool {tool_number}: {basename} === )\n")
            output_lines.extend(tc_lines)

            # Defense in depth: ensure absolute mode before operations
            output_lines.append("G90        ( Ensure absolute mode before operations )\n")

            # Add operations (strip leading dwells - we generate our own)
            ops = strip_leading_dwells(parsed['operations'])
            if ops:
                output_lines.extend(ops)

                # Ensure we end at safe height
                last_parsed = Line(ops[-1])
                z = get_z_from_line(last_parsed)
                if z is None or z < 0:
                    output_lines.append(f"G00 Z{safe_z:.5f} {COMMENT_RETRACT_AFTER}\n")
    else:
        # Same-tool mode: use original tool change from first file, concatenate operations
        output_lines.extend(parsed_files[0]['tool_change'])

        for i, parsed in enumerate(parsed_files):
            basename = os.path.basename(parsed['filepath'])

            # Add section comment
            output_lines.append(f"\n{COMMENT_SECTION.format(basename)}\n")

            # Defense in depth: ensure absolute mode
            output_lines.append("G90        ( Ensure absolute mode before operations )\n")

            # If not the first file, ensure we're at safe height
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
                    output_lines.append(f"G00 Z{tool_change_z:.5f} {COMMENT_SAFETY_RETRACT}\n")
                    output_lines.append(f"G4 P0 {COMMENT_DWELL_SYNC}\n")

                output_lines.extend(ops)

                # Ensure we end at safe height
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
    if multi_tool:
        print(f"\nCombined {len(input_files)} files with {len(input_files)} tool changes into {output_file}")
    else:
        print(f"\nCombined {len(input_files)} files into {output_file}")
    print(f"Total operation lines: {total_ops}")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="""
        Combine multiple pcb2gcode G-code files into a single file.

        By default, all files must use the same tool size (safe for single-bit workflows).
        Use --multi to allow different tools with M6 tool change pauses.

        Safety features:
        - Validates unit consistency (refuses mm + inches)
        - Detects dangerous G28/G30 commands
        - Adds explicit state header (G90 G21 G17 G94)
        - Ensures safe Z transitions between operations
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("input_files", nargs='+',
                        help="Input G-code files to combine (in order)")
    parser.add_argument("-o", "--output", required=True,
                        help="Output combined G-code file")
    parser.add_argument("--multi", action="store_true",
                        help="Allow different tools with M6 tool change sequences")

    args = parser.parse_args()

    if len(args.input_files) < 2:
        print("Error: Need at least 2 input files to combine.", file=sys.stderr)
        sys.exit(1)

    success = combine_files(args.input_files, args.output, multi_tool=args.multi)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
