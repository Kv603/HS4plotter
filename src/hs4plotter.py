import os
import sys
import re
import json
import logging
import logging.handlers
import requests
import argparse
import configparser
from pathlib import Path
from datetime import datetime, timezone

# Optional InfluxDB Support
try:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
    INFLUX_SUPPORTED = True
except ImportError:
    INFLUX_SUPPORTED = False

# --- Logging Setup ---
logger = logging.getLogger("HS4ToIoTPlotter")
logger.setLevel(logging.INFO)

syslog_path = "/dev/log" if os.path.exists("/dev/log") else "/var/run/syslog"
try:
    sh = logging.handlers.SysLogHandler(address=syslog_path)
    sh.setFormatter(logging.Formatter('%(name)s: %(message)s'))
    logger.addHandler(sh)
except Exception:
    pass

eh = logging.StreamHandler(sys.stderr)
logger.addHandler(eh)

STATUS_MAP = {"Idle": 0, "Active": 1, "Off": 0, "On": 1, "Closed": 0, "Open": 1}

def parse_hs4_date(date_str):
    try:
        match = re.search(r"Date\((\d+)", str(date_str))
        if match:
            return int(match.group(1)) / 1000.0
    except:
        return None
    return None

def extract_numeric(status_str):
    match = re.search(r"([-+]?\d*\.\d+|\d+)", str(status_str))
    return float(match.group(1)) if match else None

def strict_clean(name):
    if not name: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', str(name)).lower()

def sanitize_graph_name(name):
    name = str(name).replace(" ", "_")
    name = re.sub(r'_{2,}', '_', name)
    return name.strip("_")

def load_config():
    config = configparser.ConfigParser()
    files = [Path("./iotplot.ini"), Path.home() / "iotplot.ini"]
    for f in files:
        if f.exists():
            config.read(f)
            return config
    return None

def resolve_val(cli_val, env_name, config, section, key):
    if cli_val is not None: return cli_val
    env_val = os.getenv(env_name)
    if env_val is not None: return env_val
    if config and config.has_option(section, key):
        return config.get(section, key)
    return None

def get_hs4_data(url, user, password, refs, timeout, verbose=False):
    params = {"request": "getstatus", "ref": refs, "user": user, "pass": password}
    try:
        resp = requests.get(f"{url}/JSON", params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if verbose:
            print(f"--- DEBUG: HS4 Response ---\n{json.dumps(data, indent=2)}\n---")
        return data.get("Devices", [])
    except Exception as e:
        logger.error(f"HomeSeer Request Failed: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="HomeSeer4 to IoTPlotter & InfluxDB")
    parser.add_argument("--discover", help="Comma-separated Device IDs or Names to discover children.")
    parser.add_argument("--verbose", action="store_true", help="Detailed logs.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send data anywhere.")
    parser.add_argument("--user", help="HS4 User")
    parser.add_argument("--password", help="HS4 Pass")
    parser.add_argument("--api-key", help="IoTPlotter API Key")
    parser.add_argument("--feed-id", help="IoTPlotter Feed ID")
    parser.add_argument("--url", help="HS4 URL")
    parser.add_argument("--prefix", help="Prefix template. Use '' to disable.")
    parser.add_argument("--influx-url", help="InfluxDB URL")
    parser.add_argument("--influx-token", help="InfluxDB Token")
    parser.add_argument("--influx-org", help="InfluxDB Org")
    parser.add_argument("--influx-bucket", help="InfluxDB Bucket")
    args = parser.parse_args()

    cfg = load_config()

    # --- Configuration Resolution ---
    hs_url  = resolve_val(args.url, "HS4_URL", cfg, "homeseer", "url")
    user    = resolve_val(args.user, "HS4_USER", cfg, "homeseer", "user") or ""
    pw      = resolve_val(args.password, "HS4_PASS", cfg, "homeseer", "pass") or ""
    hs_refs = resolve_val(None, "HS4_REFS", cfg, "homeseer", "refs")
    hs_timeout = int(resolve_val(None, "HS4_TIMEOUT", cfg, "homeseer", "timeout") or 10)

    iot_api = resolve_val(args.api_key, "IOT_API_KEY", cfg, "iotplotter", "api_key")
    iot_feed = resolve_val(args.feed_id, "IOT_FEED_ID", cfg, "iotplotter", "feed_id")
    iot_max_hrs = cfg.getfloat("iotplotter", "last_change_max_hours", fallback=None) if cfg else None
    iot_timeout = int(resolve_val(None, "IOT_TIMEOUT", cfg, "iotplotter", "timeout") or 10)
    prefix_template = resolve_val(args.prefix, "IOT_PREFIX", cfg, "iotplotter", "prefix")

    inf_url = resolve_val(args.influx_url, "INFLUX_URL", cfg, "grafana", "url")
    inf_tok = resolve_val(args.influx_token, "INFLUX_TOKEN", cfg, "grafana", "token")
    inf_org = resolve_val(args.influx_org, "INFLUX_ORG", cfg, "grafana", "org")
    inf_bkt = resolve_val(args.influx_bucket, "INFLUX_BUCKET", cfg, "grafana", "bucket")
    inf_max_hrs = cfg.getfloat("grafana", "last_change_max_hours", fallback=None) if cfg else None
    # Grafana timeout in seconds, converted to ms for InfluxDB client
    inf_timeout_sec = int(resolve_val(None, "INFLUX_TIMEOUT", cfg, "grafana", "timeout") or 10)
    inf_timeout_ms = inf_timeout_sec * 1000

    # Load Exclusions & Scaling
    ignore_refs = [r.strip() for r in (cfg.get("homeseer", "ignorerefs", fallback="")).split(",") if r.strip()]
    ignore_names_cleaned = [strict_clean(n) for n in (cfg.get("homeseer", "ignorenames", fallback="")).split(",") if n.strip()]
    ignore_regex = cfg.get("homeseer", "ignoreregex", fallback=None)
    scaling_map = {k: float(v) for k, v in cfg.items("scaling")} if cfg and cfg.has_section("scaling") else {}

    if not hs_url:
        logger.error("Error: HomeSeer URL is required."); sys.exit(1)

    # Device Discovery
    discovered_refs = set()
    if args.discover:
        all_potential = get_hs4_data(hs_url, user, pw, "all", hs_timeout)
        for p_query in args.discover.split(","):
            p_query = p_query.strip()
            parent = next((d for d in all_potential if str(d.get("ref")) == p_query or d.get("name") == p_query), None)
            if parent and parent.get("associated_devices"):
                discovered_refs.update(parent["associated_devices"])
        target_refs = ",".join(map(str, discovered_refs))
    else:
        target_refs = hs_refs

    if not target_refs:
        logger.error("No reference IDs provided."); sys.exit(1)

    # Data Processing of known devices
    devices = get_hs4_data(hs_url, user, pw, target_refs, hs_timeout, args.verbose)
    iot_payload = {"data": {}}
    influx_points = []

    processed_count = 0
    iot_sent_count = 0
    stale_refs = []
    now_ts = datetime.now(timezone.utc).timestamp()

    for dev in devices:
        processed_count += 1
        ref = str(dev.get("ref"))
        orig_name = dev.get("name", "Unknown")
        status_str = str(dev.get("status", ""))
        raw_val = dev.get("value")
        epoch = parse_hs4_date(dev.get("last_change", ""))

        if ref in ignore_refs or strict_clean(orig_name) in ignore_names_cleaned: continue
        if ignore_regex and re.search(ignore_regex, orig_name, re.IGNORECASE): continue

        val = float(raw_val) if isinstance(raw_val, (int, float)) else extract_numeric(status_str)
        if val is None: val = STATUS_MAP.get(status_str)
        if val is None: continue

        if ref in scaling_map: val *= scaling_map[ref]

        # IoTPlotter logic
        if iot_api and iot_feed:
            age_hrs = (now_ts - epoch) / 3600 if epoch else 0
            if iot_max_hrs and age_hrs > iot_max_hrs:
                stale_refs.append(ref)
            else:
                mapping = {"location": dev.get("location"), "location2": dev.get("location2"),
                           "location_location2": f"{dev.get('location')}_{dev.get('location2')}",
                           "location2_location": f"{dev.get('location2')}_{dev.get('location')}"}
                prefix = mapping.get(prefix_template, prefix_template) if prefix_template else ""
                g_name = sanitize_graph_name(f"{prefix}_{orig_name}" if prefix else orig_name)

                entry = {"value": val}
                if epoch: entry["epoch"] = int(epoch)
                iot_payload["data"][g_name] = [entry]
                iot_sent_count += 1

        # InfluxDB logic
        if INFLUX_SUPPORTED and all([inf_url, inf_tok, inf_org, inf_bkt]):
            age_hrs = (now_ts - epoch) / 3600 if epoch else 0
            if not (inf_max_hrs and age_hrs > inf_max_hrs):
                p = Point("homeseer_devices").tag("name", orig_name) \
                    .tag("location", dev.get("location")) \
                    .tag("location2", dev.get("location2")) \
                    .tag("interface", dev.get("interface_name"))

                p.field("value", val)
                if status_str and not re.match(r'^\d', status_str):
                    p.tag("status", status_str)
                else:
                    p.field("status_str", status_str)

                if epoch: p.time(int(epoch), WritePrecision.S)
                influx_points.append(p)

    # Do we have data and are we not just doing a dry run?
    if iot_payload["data"] and not args.dry_run:
        try:
            iot_url = f"https://iotplotter.com/api/v2/feed/{iot_feed}"
            r = requests.post(iot_url, headers={"api-key": iot_api, "Content-Type": "application/json"}, json=iot_payload, timeout=iot_timeout)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"IoTPlotter failed: {e}")

    if influx_points and not args.dry_run:
        try:
            with InfluxDBClient(url=inf_url, token=inf_tok, org=inf_org, timeout=inf_timeout_ms) as client:
                client.write_api(write_options=SYNCHRONOUS).write(bucket=inf_bkt, record=influx_points)
        except Exception as e:
            logger.error(f"InfluxDB failed: {e}")

    summary = (f"Execution Summary: Processed={processed_count}, SentToIoT={iot_sent_count}, "
               f"SentToInflux={len(influx_points)}, StaleRefs={','.join(stale_refs) if stale_refs else 'None'}")

    #logger.info(summary)
    if args.verbose: print(f"Verbose \n{summary}")

if __name__ == "__main__":
    main()
