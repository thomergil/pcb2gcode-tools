#!/usr/bin/env python3
"""
pcb2gcode-wrapper

Wrapper script that runs pcb2gcode three times with the specified basename:
1. For back copper, --back on [basename]-B_Cu.gbr
2. For drill holes, --drill on [basename]-PTH.drl
3. For board outline, --outline on [basename]-Edge_Cuts.gbr

Automatically calculates offsets from the Edge_Cuts.gbr file and runs
pcb2gcode-fixup and pcb2gcode-combine on the output files.
"""

import argparse
import math
import os
import re
import shutil
import subprocess
import sys

# Default margins: additional space added to board dimensions for offset calculations
DEFAULT_X_MARGIN = 5
DEFAULT_Y_MARGIN = 3

# Tool commands - these are entry points from the same package
FIXUP_CMD = 'pcb2gcode-fixup'
COMBINE_CMD = 'pcb2gcode-combine'


def parse_fslax_format(line):
    """Parse FSLAX format specification from Gerber header."""
    match = re.match(r'%FSLAX(\d)(\d)Y(\d)(\d)\*%', line)
    if not match:
        return None
    x_decimal = int(match.group(2))
    units_factor = 10.0 ** x_decimal
    print(f"Detected Gerber format: {match.group(1)}.{x_decimal}, units factor: {units_factor}")
    return units_factor


def parse_gerber_units(line, units_factor, millimeter_units):
    """Parse units specification from Gerber header line."""
    if re.match(r'%FSLAX(\d)(\d)Y(\d)(\d)\*%', line):
        new_factor = parse_fslax_format(line)
        if new_factor:
            units_factor = new_factor
    elif '%MOMM*%' in line:
        print('Detected millimeter units in Gerber file')
        units_factor = 1_000_000.0
        millimeter_units = True
    elif '%MOIN*%' in line:
        print('Detected inch units in Gerber file')
    return units_factor, millimeter_units


def update_coordinate_bounds(line, xmin, xmax, ymin, ymax):
    """Extract coordinates from Gerber line and update bounds."""
    match = re.match(r'^X([\d-]+)Y([\d-]+)', line)
    if not match:
        return xmin, xmax, ymin, ymax

    x = int(match.group(1))
    y = int(match.group(2))

    xmin = min(xmin, x) if xmin is not None else x
    xmax = max(xmax, x) if xmax is not None else x
    ymin = min(ymin, y) if ymin is not None else y
    ymax = max(ymax, y) if ymax is not None else y

    return xmin, xmax, ymin, ymax


def extract_coordinates(filename):
    """Extract coordinates from Gerber file."""
    xmin = xmax = ymin = ymax = None
    units_factor = 10_000.0  # Default: decimills to inches
    millimeter_units = False

    with open(filename, 'r') as f:
        for line in f:
            units_factor, millimeter_units = parse_gerber_units(line, units_factor, millimeter_units)
            xmin, xmax, ymin, ymax = update_coordinate_bounds(line, xmin, xmax, ymin, ymax)

    return xmin, xmax, ymin, ymax, units_factor, millimeter_units


def convert_to_inches(width, height, units_factor, millimeter_units):
    """Convert dimensions to inches based on detected format."""
    if millimeter_units:
        width_mm = width / units_factor
        height_mm = height / units_factor
        print(f"Board dimensions in mm: {width_mm:.2f}mm x {height_mm:.2f}mm")
        return width_mm / 25.4, height_mm / 25.4
    return width / units_factor, height / units_factor


def parse_gerber_dimensions(filename, x_margin):
    """Parse Gerber file and extract board dimensions, return calculated x-offset."""
    if not os.path.exists(filename):
        return None

    xmin, xmax, ymin, ymax, units_factor, millimeter_units = extract_coordinates(filename)
    if xmin is None or xmax is None:
        return None

    width = xmax - xmin
    height = ymax - ymin
    width_inches, height_inches = convert_to_inches(width, height, units_factor, millimeter_units)

    width_mm = width_inches * 25.4
    height_mm = height_inches * 25.4
    print(f"Board dimensions detected: {width_inches:.4f}\" x {height_inches:.4f}\" "
          f"({width_mm:.2f}mm x {height_mm:.2f}mm)")

    # Calculate x-offset (negative, based on width + margin)
    offset = -1 * (math.ceil(width_mm) + x_margin)
    print(f"Calculated x-offset: {offset} (based on width: {width_mm:.2f}mm + {x_margin}mm x-margin)")
    return offset


def command_available(cmd):
    """Check if a command is available in PATH."""
    return shutil.which(cmd) is not None


def run_command(cmd, description=None):
    """Run a command and handle errors."""
    if description:
        print(f"\n{description}...")
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"Error executing command: {cmd}", file=sys.stderr)
        sys.exit(1)


def run_fixup(input_file, output_dir, fixup_available):
    """Run pcb2gcode-fixup on an NGC file if available."""
    if not fixup_available:
        return

    # Prepend output directory if specified
    full_input_path = os.path.join(output_dir, input_file) if output_dir else input_file

    # Check if file exists
    if os.path.exists(full_input_path):
        output_file = full_input_path.replace('.ngc', '-fixup.ngc')
    elif os.path.exists(input_file):
        print(f"Debug: Found {input_file} in current directory (output-dir may not be working)")
        full_input_path = input_file
        output_file = input_file.replace('.ngc', '-fixup.ngc')
    else:
        dir_desc = output_dir if output_dir else 'current directory'
        print(f"Warning: {input_file} not found in {dir_desc}")
        return

    cmd = f"{FIXUP_CMD} --remove-m6 {full_input_path} {output_file}"
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"Warning: {FIXUP_CMD} failed on {full_input_path}")


def main():
    parser = argparse.ArgumentParser(
        description="""
Wrapper script that runs pcb2gcode three times with the specified basename:
1. For back copper, --back on [basename]-B_Cu.gbr
2. For drill holes, --drill on [basename]-PTH.drl
3. For board outline, --outline on [basename]-Edge_Cuts.gbr

The --x-offset parameter is automatically calculated from edge cuts if present.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('basename', help='Base name of the Gerber/drill files')
    parser.add_argument('--x-margin', type=float, default=DEFAULT_X_MARGIN,
                        help=f'X margin (mm) for x-offset calculation (default: {DEFAULT_X_MARGIN})')
    parser.add_argument('--y-margin', type=float, default=DEFAULT_Y_MARGIN,
                        help=f'Y margin (mm) for y-offset (default: {DEFAULT_Y_MARGIN})')
    parser.add_argument('--no-combine', action='store_true',
                        help='Skip combining drill/milldrill/outline into single file')
    parser.add_argument('--output-dir', metavar='DIR',
                        help='Output directory for generated files')
    parser.add_argument('--x-offset', type=float, metavar='MM',
                        help='Override automatic x-offset calculation')
    parser.add_argument('--y-offset', type=float, metavar='MM',
                        help='Override automatic y-offset calculation')

    # Parse known args, pass the rest to pcb2gcode
    args, extra_args = parser.parse_known_args()

    basename = args.basename
    other_args = ' '.join(extra_args)

    # Handle output directory
    output_dir = ''
    if args.output_dir:
        output_dir = os.path.expanduser(args.output_dir).rstrip('/')
        print(f"Output directory: {output_dir}")
        if not os.path.isdir(output_dir):
            print(f"Error: Output directory '{output_dir}' does not exist.", file=sys.stderr)
            sys.exit(1)
        other_args += f" --output-dir={output_dir}"

    # Calculate x-offset from edge cuts file
    edge_cuts_file = f"{basename}-Edge_Cuts.gbr"
    if args.x_offset is not None:
        x_offset = args.x_offset
        print(f"Using provided x-offset: {x_offset}")
    else:
        x_offset = parse_gerber_dimensions(edge_cuts_file, args.x_margin)
        if x_offset is None and not os.path.exists(edge_cuts_file):
            print(f"Warning: {edge_cuts_file} not found, cannot auto-calculate x-offset")

    # Add x-offset to args
    if x_offset is not None:
        other_args += f" --x-offset={x_offset}"
        print(f"Automatically added --x-offset={x_offset} to commands")

    # Add y-offset to args
    if args.y_offset is not None:
        y_offset = args.y_offset
    else:
        y_offset = args.y_margin
    other_args += f" --y-offset={y_offset}"

    # Check for helper tools
    fixup_available = command_available(FIXUP_CMD)
    print(f"{FIXUP_CMD} {'found' if fixup_available else 'not found'} in PATH")

    combine_available = command_available(COMBINE_CMD)
    print(f"{COMBINE_CMD} {'found' if combine_available else 'not found'} in PATH")

    # Run back copper
    back_cmd = f"pcb2gcode --back {basename}-B_Cu.gbr --basename {basename} {other_args}"
    run_command(back_cmd, "Processing back copper")
    run_fixup(f"{basename}_back.ngc", output_dir, fixup_available)

    # Run drill
    drill_cmd = f"pcb2gcode --drill {basename}-PTH.drl --drill-side back --basename {basename} {other_args}"
    run_command(drill_cmd, "Processing drill holes")
    run_fixup(f"{basename}_drill.ngc", output_dir, fixup_available)
    run_fixup(f"{basename}_milldrill.ngc", output_dir, fixup_available)

    # Run outline
    outline_cmd = f"pcb2gcode --outline {basename}-Edge_Cuts.gbr --cut-side back --basename {basename} {other_args}"
    run_command(outline_cmd, "Processing board outline")
    run_fixup(f"{basename}_outline.ngc", output_dir, fixup_available)

    # Combine drill, milldrill, and outline into a single file
    if not args.no_combine:
        if combine_available:
            prefix = f"{output_dir}/" if output_dir else ""
            suffix = "-fixup.ngc" if fixup_available else ".ngc"

            drill_file = f"{prefix}{basename}_drill{suffix}"
            milldrill_file = f"{prefix}{basename}_milldrill{suffix}"
            outline_file = f"{prefix}{basename}_outline{suffix}"
            combined_file = f"{prefix}{basename}_01_combined.ngc"

            # Check which files exist
            input_files = [f for f in [drill_file, milldrill_file, outline_file] if os.path.exists(f)]

            if len(input_files) >= 2:
                print("\nCombining drill operations...")
                cmd = f"{COMBINE_CMD} {' '.join(input_files)} -o {combined_file}"
                print(f"Running: {cmd}")
                result = subprocess.run(cmd, shell=True)
                if result.returncode == 0:
                    print(f"Created combined file: {combined_file}")

                    # Rename back file to sort first
                    back_file = f"{prefix}{basename}_back{suffix}"
                    back_file_renamed = f"{prefix}{basename}_00_back.ngc"
                    if os.path.exists(back_file):
                        os.rename(back_file, back_file_renamed)
                        print(f"Renamed: {back_file} -> {back_file_renamed}")
                else:
                    print(f"Warning: {COMBINE_CMD} failed")
            else:
                print(f"Warning: Need at least 2 files to combine, found {len(input_files)}")
        else:
            print(f"\nSkipping combine: {COMBINE_CMD} not found in PATH")

    print("\nAll operations completed successfully!")


if __name__ == "__main__":
    main()
