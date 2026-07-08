import argparse
import concurrent.futures
import logging
import threading

import pandas as pd
import re
from bs4 import BeautifulSoup
from pathlib import Path
import os

def parse_acv_report(file_path):
    """Parses a single XHTML report file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            # ACVTool output is HTML-like but not strict XML; use the HTML parser for robustness
            soup = BeautifulSoup(f, 'lxml')

        # 1. Extract Column Headers
        thead = soup.find('thead')
        headers = []
        if thead:
            # Header cells are typically inside a <tr> under <thead>; find that tr first
            tr_head = thead.find('tr')
            if tr_head:
                headers = [cell.get_text(separator=' ', strip=True) for cell in tr_head.find_all(['th', 'td'], recursive=False)]

        data_rows = []

        # 2. Parse the Body (individual entries)
        tbody = soup.find('tbody')
        if tbody:
            # iterate only top-level rows to avoid nested tables
            # helper to extract cell text; special-case the 'bar' column which contains
            # <img> elements with title/alt attributes holding numeric counts (red/green)
            def _extract_cell_text(td):
                # if images are present, build a "green of total" ratio string
                imgs = td.find_all('img')
                if imgs:
                    red = None
                    green = None
                    vals = []
                    for img in imgs:
                        t = img.get('title') or img.get('alt') or ''
                        # try to extract integer from title/alt
                        try:
                            v = int(re.sub(r'[^0-9]', '', t)) if t else 0
                        except Exception:
                            v = 0
                        vals.append(v)
                        cls = img.get('class') or []
                        cls_str = ' '.join(cls) if isinstance(cls, (list, tuple)) else str(cls)
                        if 'green' in cls_str.lower():
                            green = v
                        elif 'red' in cls_str.lower():
                            red = v

                    # fallback to positional mapping if classes not present/recognisable
                    if red is None or green is None:
                        if len(vals) >= 2:
                            # commonly red then green
                            red = vals[0]
                            green = vals[1]
                        elif len(vals) == 1:
                            # assume single image is covered (green)
                            green = vals[0]
                            red = 0
                        else:
                            red = red or 0
                            green = green or 0

                    total = (green or 0) + (red or 0)
                    # present ratio as string like "<green> of <total>" to match footer
                    return f"{green or 0} of {total}"

                # default: return textual content
                return td.get_text(separator=' ', strip=True)

            for tr in tbody.find_all('tr', recursive=False):
                tds = tr.find_all('td', recursive=False)
                cells = [_extract_cell_text(td) for td in tds]
                if not cells:
                    continue
                # Normalize row length to match headers (truncate or pad)
                if headers:
                    if len(cells) > len(headers):
                        cells = cells[:len(headers)]
                    elif len(cells) < len(headers):
                        cells.extend([''] * (len(headers) - len(cells)))
                data_rows.append(cells)

        # 3. Parse the Footer (Total summary)
        tfoot = soup.find('tfoot')
        if tfoot:
            footer_tds = tfoot.find_all('td', recursive=False)
            footer_cells = [td.get_text(separator=' ', strip=True) for td in footer_tds]
            if footer_cells:
                if headers:
                    if len(footer_cells) > len(headers):
                        footer_cells = footer_cells[:len(headers)]
                    elif len(footer_cells) < len(headers):
                        footer_cells.extend([''] * (len(headers) - len(footer_cells)))
                data_rows.append(footer_cells)

        # If headers could not be determined from thead, attempt to infer from the first tbody row
        if not headers:
            if tbody:
                first_tr = tbody.find('tr', recursive=False)
                if first_tr:
                    header_cells = first_tr.find_all(['th', 'td'], recursive=False)
                    if header_cells:
                        headers = [c.get_text(separator=' ', strip=True) for c in header_cells]
                        # If the first parsed data row equals the header row we just extracted, drop it
                        maybe_row = [c.get_text(separator=' ', strip=True) for c in header_cells]
                        if data_rows and data_rows[0] == maybe_row:
                            data_rows = data_rows[1:]

        # As a last resort, if we still have no headers but have rows, synthesize column names
        if not headers:
            if data_rows:
                max_cols = max(len(r) for r in data_rows)
                headers = [f'col_{i+1}' for i in range(max_cols)]
            else:
                # nothing to build
                return None

        # Normalize rows to match headers again (in case headers were inferred)
        normalized = []
        for cells in data_rows:
            if len(cells) > len(headers):
                normalized.append(cells[:len(headers)])
            elif len(cells) < len(headers):
                cells_extended = list(cells) + [''] * (len(headers) - len(cells))
                normalized.append(cells_extended)
            else:
                normalized.append(cells)

        # Create DataFrame for this specific file
        try:
            df = pd.DataFrame(normalized, columns=headers)
        except Exception:
            logging.exception('Failed to construct DataFrame for %s (headers=%d rows=%d)', file_path, len(headers), len(normalized))
            raise

        # Add metadata: where did this data come from?
        df['Source_Path'] = str(file_path)

        # Extract package name from breadcrumb (first anchor text inside ul.breadcrumb)
        package_name = None
        try:
            breadcrumb = soup.find('ul', class_='breadcrumb')
            if breadcrumb:
                a = breadcrumb.find('a')
                if a and a.get_text(strip=True):
                    package_name = a.get_text(strip=True)
        except Exception:
            logging.debug('Failed to extract package name from breadcrumb for %s', file_path)

        return df, package_name

    except Exception:
        logging.exception('Error parsing %s', file_path)
        return None


def process_directory(base_folder, out_dir='./out'):
    """Recursively searches for main_index.html files, converts each to a CSV stored
    under ``out_dir``, and returns a merged DataFrame of all parsed files.

    This is the legacy single-threaded recursive processor (keeps compatibility).
    """
    path_root = Path(base_folder)
    all_dfs = []

    # Recursively find all files named 'main_index.html'
    files = list(path_root.rglob("main_index.html"))
    total = len(files)
    logging.info('Found %d report files. Processing...', total)
    logging.debug('Files found: %s', files)

    # Ensure output directory exists
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        logging.exception('Failed to create output directory: %s', out_dir)

    for idx, file in enumerate(files, start=1):
        if idx % 50 == 0 or idx == total:
            logging.info(f"Processing reports: {idx}/{total}")
        report_res = parse_acv_report(file)
        if not report_res:
            logging.debug('Skipping file (no table found or parse error): %s', file)
            continue
        report_df, package_name = report_res
        if report_df is None:
            logging.debug('Skipping file (no table found or parse error): %s', file)
            continue

        # Attempt to infer firmware id from path (look for acv_reports or acv_snaps parent)
        firmware_id = ''
        try:
            parts = Path(file).parts
            if 'acv_reports' in parts:
                idx2 = parts.index('acv_reports')
                if idx2 > 0:
                    firmware_id = parts[idx2 - 1]
            elif 'acv_snaps' in parts:
                idx2 = parts.index('acv_snaps')
                if idx2 > 0:
                    firmware_id = parts[idx2 - 1]
        except Exception:
            firmware_id = ''

        # attach firmware id column
        report_df['Firmware_ID'] = firmware_id

        # Determine CSV filename using package name when available
        if package_name:
            # sanitize package name for filename
            pkg_safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', package_name)
            base_csv_name = f"{pkg_safe}.csv"
        else:
            # Fallback to path-based name
            try:
                rel = Path(file).relative_to(path_root)
            except Exception:
                rel = Path(file).name
            safe_name = str(rel).replace(os.path.sep, '__')
            if safe_name.lower().endswith('.html'):
                safe_name = safe_name[:-5]
            base_csv_name = f"{safe_name}.csv"

        csv_path = os.path.join(out_dir, base_csv_name)
        # Avoid overwriting if multiple reports for the same package exist: append a suffix
        if os.path.exists(csv_path):
            i = 1
            while True:
                alt = os.path.join(out_dir, f"{os.path.splitext(base_csv_name)[0]}_{i}.csv")
                if not os.path.exists(alt):
                    csv_path = alt
                    break
                i += 1

        try:
            report_df.to_csv(csv_path, index=False)
            logging.info('Wrote CSV for %s -> %s', file, csv_path)
        except Exception:
            logging.exception('Failed to write CSV for %s to %s', file, csv_path)

        all_dfs.append(report_df)

    if not all_dfs:
        logging.warning('No data found in any report files under %s', base_folder)
        return pd.DataFrame()

    # Merge all dataframes into one and return
    master_df = pd.concat(all_dfs, ignore_index=True)
    return master_df


def process_collected_reports(emulator_out, out_dir='./out', workers=8):
    """Process reports collected under <emulator_out>/<firmware>/acv_reports/<package>/main_index.html

    This function parallelizes parsing across worker threads and writes per-package CSVs
    safely using a threading lock. Returns the merged DataFrame.
    """
    root = Path(emulator_out)
    tasks = []
    # discover main_index.html files under acv_reports
    for fw_dir in sorted(root.iterdir() if root.exists() else []):
        acv_reports = fw_dir / 'acv_reports'
        if not acv_reports.is_dir():
            continue
        for pkg_dir in sorted(acv_reports.iterdir()):
            if not pkg_dir.is_dir():
                continue
            index = pkg_dir / 'main_index.html'
            if not index.exists():
                # Some reports may be named index.html; try that too
                index = pkg_dir / 'index.html'
                if not index.exists():
                    continue
            tasks.append(index)

    logging.info('Discovered %d collected report files under %s', len(tasks), emulator_out)

    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        logging.exception('Failed to create output directory: %s', out_dir)

    lock = threading.Lock()
    all_dfs = []

    def worker(file_path):
        res = parse_acv_report(file_path)
        if not res:
            logging.debug('Skipping (parse returned nothing): %s', file_path)
            return None
        df, package_name = res
        if df is None:
            logging.debug('Skipping (no dataframe): %s', file_path)
            return None

        # infer firmware id from file path relative to root
        firmware_id = ''
        try:
            rel = file_path.relative_to(root)
            if len(rel.parts) > 0:
                firmware_id = rel.parts[0]
        except Exception:
            # fallback: try to locate 'acv_reports' segment
            try:
                parts = file_path.parts
                if 'acv_reports' in parts:
                    idx = parts.index('acv_reports')
                    if idx > 0:
                        firmware_id = parts[idx - 1]
            except Exception:
                firmware_id = ''

        # attach firmware id column
        df['Firmware_ID'] = firmware_id

        # Build CSV filename
        if package_name:
            pkg_safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', package_name)
            base_csv_name = f"{pkg_safe}.csv"
        else:
            # fallback to path-based
            try:
                rel = file_path.relative_to(root)
            except Exception:
                rel = file_path.name
            safe_name = str(rel).replace(os.path.sep, '__')
            if safe_name.lower().endswith('.html'):
                safe_name = safe_name[:-5]
            base_csv_name = f"{safe_name}.csv"

        csv_path = os.path.join(out_dir, base_csv_name)

        # Write CSV with lock to avoid races and to ensure unique names
        with lock:
            if os.path.exists(csv_path):
                i = 1
                while True:
                    alt = os.path.join(out_dir, f"{os.path.splitext(base_csv_name)[0]}_{i}.csv")
                    if not os.path.exists(alt):
                        csv_path = alt
                        break
                    i += 1
            try:
                df.to_csv(csv_path, index=False)
                logging.info('Wrote CSV for %s -> %s', file_path, csv_path)
            except Exception:
                logging.exception('Failed to write CSV for %s to %s', file_path, csv_path)

        return df

    # Parallel execution with progress logging
    results = []
    total = len(tasks)
    logging.info('Starting parsing with %d worker(s) for %d task(s)', workers, total)
    processed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(worker, p) for p in tasks]
        for fut in concurrent.futures.as_completed(futures):
            processed += 1
            try:
                r = fut.result()
            except Exception:
                logging.exception('Worker exception')
                r = None
            if r is not None:
                all_dfs.append(r)
            if processed % 50 == 0 or processed == total:
                logging.info(f"Parsing progress: {processed}/{total} completed")

    if not all_dfs:
        logging.warning('No data parsed from collected reports under %s', emulator_out)
        return pd.DataFrame()

    master_df = pd.concat(all_dfs, ignore_index=True)
    return master_df


def main():
    parser = argparse.ArgumentParser(description='Parse ACVTool XHTML coverage reports and merge into a single table')
    parser.add_argument('path', nargs='?', default='.', help='Root folder to search for main_index.html files (default: .)')
    parser.add_argument('--reports-root', type=str, help='Path to emulator_out root where collected reports live (emulator_out/<fw>/acv_reports/<pkg>/)')
    parser.add_argument('--workers', type=int, default=32, help='Number of worker threads for parallel parsing when --reports-root is used')
    parser.add_argument('--csv', type=str, help='Write merged results to CSV file')
    parser.add_argument('--out-dir', type=str, default=os.path.join(os.path.dirname(__file__), 'out'), help='Directory to write per-file CSVs (default: ./out next to this script)')
    parser.add_argument('--verbose', action='store_true', help='Enable verbose (debug) logging')

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')

    if args.reports_root:
        logging.info('Processing collected reports under: %s', args.reports_root)
        final_df = process_collected_reports(args.reports_root, out_dir=args.out_dir, workers=args.workers)
    else:
        final_df = process_directory(args.path, out_dir=args.out_dir)

    if final_df.empty:
        logging.info('Merged dataframe is empty; no output produced.')
        return

    logging.info('Merged Coverage Data (first rows):\n%s', final_df.head().to_string(index=False))

    if args.csv:
        try:
            final_df.to_csv(args.csv, index=False)
            logging.info('Exported merged results to CSV: %s', args.csv)
        except Exception:
            logging.exception('Failed to write CSV to %s', args.csv)

if __name__ == '__main__':
    main()
