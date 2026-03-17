import subprocess
import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def run_acv_command(cmd_args):
    """
    Run an ACVTool command and return output, error, and exit code.
    """
    try:
        logging.info(f"Running command: {' '.join(cmd_args)}")
        result = subprocess.run(cmd_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            logging.error(f"Command failed: {' '.join(cmd_args)}")
            logging.error(result.stderr)
        else:
            logging.info(result.stdout)
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        logging.error(f"Exception running command: {e}")
        return 1, '', str(e)


def activate_app(package):
    return run_acv_command(['acv', 'activate', package])


def snap_coverage(package, wd=None):
    cmd = ['acv', 'snap', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def cover_pickles(package, wd=None):
    cmd = ['acv', 'cover-pickles', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def generate_report(package, wd=None):
    cmd = ['acv', 'report', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def flush_coverage(package, wd=None):
    cmd = ['acv', 'flush', package]
    if wd:
        cmd += ['--wd', wd]
    return run_acv_command(cmd)


def main():
    parser = argparse.ArgumentParser(description='ACVTool Wrapper CLI')
    subparsers = parser.add_subparsers(dest='command', required=True)

    parser_activate = subparsers.add_parser('activate', help='Activate app')
    parser_activate.add_argument('package', help='App package name')

    parser_snap = subparsers.add_parser('snap', help='Take coverage snapshot')
    parser_snap.add_argument('package', help='App package name')
    parser_snap.add_argument('--wd', required=False, help='Working directory (optional)')

    parser_flush = subparsers.add_parser('flush', help='Reset instruction tracking for an app')
    parser_flush.add_argument('package', help='App package name')
    parser_flush.add_argument('--wd', required=False, help='Working directory (optional)')

    parser_cover = subparsers.add_parser('cover-pickles', help='Apply coverage data to Smali code tree')
    parser_cover.add_argument('package', help='App package name')
    parser_cover.add_argument('--wd', required=False, help='Working directory (optional)')

    parser_report = subparsers.add_parser('report', help='Generate coverage report')
    parser_report.add_argument('package', help='App package name')
    parser_report.add_argument('--wd', required=False, help='Working directory (optional)')

    args = parser.parse_args()

    if args.command == 'activate':
        activate_app(args.package)
    elif args.command == 'snap':
        snap_coverage(args.package, args.wd)
    elif args.command == 'flush':
        flush_coverage(args.package, args.wd)
    elif args.command == 'cover-pickles':
        cover_pickles(args.package, args.wd)
    elif args.command == 'report':
        generate_report(args.package, args.wd)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
