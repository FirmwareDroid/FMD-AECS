import os
import json
import datetime
import logging

logger = logging.getLogger(__name__)


def append_run(tool_name: str, summary: dict, failures: list, out_dir: str | None = None):
    """Append a run entry to a tool-specific summary file.

    The file format mirrors app_start_summary.json used by start_apps: a top-level
    JSON object with 'runs' (list) and 'aggregate' (summary across runs).

    :param tool_name: short name used to name the file, e.g. 'ape', 'combodroid'
    :param summary: a summary dict for this run (same shape as start_packages summary)
    :param failures: list of failure dicts
    :param out_dir: directory to store the summary file. If None, write to a local
                    'output' directory next to this module.
    """
    if out_dir is None:
        base = os.path.dirname(os.path.abspath(__file__))
        out_dir = os.path.join(base, 'output')
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f'{tool_name}_summary.json')

    run_entry = {
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z',
        'summary': summary,
        'failures': failures,
    }

    runs = []
    if os.path.exists(out_path):
        try:
            with open(out_path, 'r', encoding='utf-8') as rf:
                existing = json.load(rf)
            if isinstance(existing, dict) and isinstance(existing.get('runs'), list):
                runs = existing.get('runs', [])
            else:
                # migrate legacy single summary
                try:
                    mtime = os.path.getmtime(out_path)
                    prev_ts = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).isoformat() + 'Z'
                except Exception:
                    prev_ts = None
                prev_entry = {
                    'timestamp': prev_ts,
                    'summary': existing if isinstance(existing, dict) else {'legacy_summary': existing},
                    'failures': existing.get('failures') if isinstance(existing, dict) else None,
                    'migrated_from': 'legacy_single_summary',
                }
                runs = [prev_entry]
        except Exception:
            logger.exception('Failed to read existing summary file; starting a new runs list')

    runs.append(run_entry)

    # compute aggregate
    total_tested = 0
    total_started = 0
    total_failed = 0
    total_skipped = 0
    total_started_by_script = 0
    started_pkgs_set = set()
    failed_pkgs_set = set()
    skipped_pkgs_set = set()
    combined_failure_freq = {}

    for r in runs:
        s = r.get('summary') or {}
        total_tested += int(s.get('total_packages', 0) or 0)
        total_started += int(s.get('started', 0) or 0)
        total_failed += int(s.get('failed', 0) or 0)
        total_skipped += int(s.get('skipped', 0) or 0)
        total_started_by_script += int(s.get('started_by_script', 0) or 0)
        for p in s.get('started_packages', []) or []:
            try:
                started_pkgs_set.add(p)
            except Exception:
                pass
        for p in s.get('failed_packages', []) or []:
            try:
                failed_pkgs_set.add(p)
            except Exception:
                pass
        for p in s.get('skipped_packages', []) or []:
            try:
                skipped_pkgs_set.add(p)
            except Exception:
                pass
        ff = s.get('failure_frequency') or {}
        if isinstance(ff, dict):
            for reason, cnt in ff.items():
                try:
                    combined_failure_freq[reason] = combined_failure_freq.get(reason, 0) + int(cnt or 0)
                except Exception:
                    combined_failure_freq[reason] = combined_failure_freq.get(reason, 0)

    aggregate = {
        'total_runs': len(runs),
        'total_packages_tested': total_tested,
        'total_started': total_started,
        'total_failed': total_failed,
        'total_skipped': total_skipped,
        'total_started_by_script': total_started_by_script,
        'unique_started_packages': sorted(list(started_pkgs_set)),
        'unique_failed_packages': sorted(list(failed_pkgs_set)),
        'unique_skipped_packages': sorted(list(skipped_pkgs_set)),
        'combined_failure_frequency': combined_failure_freq,
    }

    tmp_path = out_path + '.tmp'
    try:
        with open(tmp_path, 'w', encoding='utf-8') as wf:
            json.dump({'runs': runs, 'aggregate': aggregate}, wf, indent=2)
            wf.flush()
            try:
                os.fsync(wf.fileno())
            except Exception:
                pass
        os.replace(tmp_path, out_path)
        logger.info('Wrote tool summary to %s (total runs: %d)', out_path, len(runs))
    except Exception:
        logger.exception('Failed to write tool summary to %s', out_path)

