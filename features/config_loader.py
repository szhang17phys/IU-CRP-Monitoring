"""
Configuration loader for WLC High Bay Monitoring.

Loads settings from config.local.yaml (user-specific, gitignored) or config.yaml (example).
Falls back to hardcoded defaults if no config file exists (backwards compatible).
"""

import os
import yaml


def load_config(base_dir):
    """
    Load configuration with fallback chain:
    1. config.local.yaml (user-specific, highest priority)
    2. config.yaml (example/default)
    3. Hardcoded defaults (backwards compatible)

    Returns dict with all configuration values.
    """
    config_files = [
        os.path.join(base_dir, 'config.local.yaml'),
        os.path.join(base_dir, 'config.yaml'),
    ]

    for cf in config_files:
        if os.path.exists(cf):
            try:
                with open(cf, 'r') as f:
                    config = yaml.safe_load(f)
                    if config is not None:
                        print(f"[CONFIG] Loaded from {os.path.basename(cf)}")
                        return config
            except Exception as e:
                print(f"[CONFIG] WARNING: Failed to load {cf}: {e}")
                continue

    # No config file found — return hardcoded defaults (current behavior)
    print("[CONFIG] No config file found — using hardcoded defaults")
    return get_default_config()


def get_default_config():
    """
    Return hardcoded default configuration.
    These values match the current particle_plus.py defaults.
    """
    return {
        'counter': {
            'ip': '10.66.66.68',
            'port': 502,
            'password': '',
        },
        'paths': {
            'project_data_dir': '~/particle_data',  # Safe default: home directory
        },
        'sampling': {
            'sample_time_s': 60,
            'hold_time_s': 0,
            'delay_time_s': 5,
            'cycles': 0,
        },
        'sync': {
            'erase_after_sync': False,
            'min_records_to_sync': 1,
            'trim_cap': 20000,
        },
        'github': {
            'enabled': False,  # Safe default: monitoring only (no auto-push)
            'repo_dir': None,  # None = use BASE_DIR
            'branch': 'main',
            'remote': 'origin',
            'push_interval_s': 300,
        },
        'thresholds': {
            'temp_low_f': 32,
            'temp_high_f': 110,
            'rh_low_pct': 30,
            'rh_high_pct': 70,
            'env_warn_margin': 5,
        },
        'metadata': {
            'institution': 'Yale University',
            'location': 'Wright Lab DUNE High Bay',
            'dashboard_title': 'DUNE CRP Assembly Site',
        },
    }
