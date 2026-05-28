from pymodbus.client import ModbusTcpClient
import struct
import csv
import time
from datetime import datetime

COUNTER_IP = '10.66.114.55'
PORT = 502
POLL_INTERVAL = 5  # seconds between readings
OUTPUT_CSV = 'particle_data.csv'

def decode_float(registers):
    """Convert two 16-bit registers to IEEE 754 float"""
    raw = struct.pack('>HH', registers[1], registers[0])
    return struct.unpack('>f', raw)[0]

def read_all_data(client):
    data = {}
    data['timestamp'] = datetime.now().isoformat()

    # --- sampling state ---
    r = client.read_holding_registers(address=5000, count=1)
    if not r.isError():
        state_map = {0:'Stopped', 1:'Delay', 2:'Counting', 3:'Hold'}
        data['state'] = state_map.get(r.registers[0], 'Unknown')

    # --- latch current data ---
    client.write_registers(address=8002, values=[0, 0])
    time.sleep(0.5)

    # --- temperature (LSB = 0.1 C) ---
    r = client.read_holding_registers(address=9079, count=1)
    if not r.isError():
        raw = r.registers[0]
        if raw == 999:
            data['temp_C'] = 'No sensor'
        elif raw == 998:
            data['temp_C'] = 'Sensor error'
        else:
            data['temp_C'] = round(raw * 0.1, 1)

    # --- relative humidity ---
    r = client.read_holding_registers(address=9080, count=1)
    if not r.isError():
        raw = r.registers[0]
        if raw == 0:
            data['RH_pct'] = 'No sensor'
        elif raw == 1:
            data['RH_pct'] = 'Sensor error'
        else:
            data['RH_pct'] = raw

    # --- sample duration and flow ---
    r = client.read_holding_registers(address=9074, count=2)
    if not r.isError():
        data['sample_duration_s'] = round(decode_float(r.registers), 2)

    r = client.read_holding_registers(address=9076, count=2)
    if not r.isError():
        data['flow_CFM'] = round(decode_float(r.registers), 4)

    # --- 6 particle channels ---
    # base addresses from register map:
    # 10300 = differential counts
    # 10500 = differential counts/ft3
    # 10700 = differential counts/m3
    # 11100 = cumulative counts
    # 11500 = cumulative counts/m3

    channel_sizes = ['ch1', 'ch2', 'ch3', 'ch4', 'ch5', 'ch6']

    for i, ch in enumerate(channel_sizes):
        offset = i * 2  # each channel takes 2 registers (float)

        # channel size in microns
        r = client.read_holding_registers(address=10100 + offset, count=2)
        if not r.isError():
            data[f'{ch}_size_um'] = round(decode_float(r.registers), 2)

        # differential counts
        r = client.read_holding_registers(address=10300 + offset, count=2)
        if not r.isError():
            data[f'{ch}_diff_counts'] = round(decode_float(r.registers), 1)

        # differential counts/m3
        r = client.read_holding_registers(address=10700 + offset, count=2)
        if not r.isError():
            data[f'{ch}_diff_m3'] = round(decode_float(r.registers), 2)

        # cumulative counts/m3
        r = client.read_holding_registers(address=11500 + offset, count=2)
        if not r.isError():
            data[f'{ch}_sum_m3'] = round(decode_float(r.registers), 2)

    return data

def write_csv(data, filepath):
    file_exists = False
    try:
        with open(filepath, 'r'):
            file_exists = True
    except FileNotFoundError:
        pass

    with open(filepath, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=data.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(data)

def main():
    print(f"Starting particle counter logger")
    print(f"Polling every {POLL_INTERVAL}s → {OUTPUT_CSV}")
    print("Press Ctrl+C to stop\n")

    client = ModbusTcpClient(COUNTER_IP, port=PORT, timeout=5)

    while True:
        try:
            if not client.connect():
                print(f"{datetime.now()} - Connection failed, retrying...")
                time.sleep(10)
                continue

            data = read_all_data(client)
            write_csv(data, OUTPUT_CSV)

            print(f"{data['timestamp']} | "
                  f"State: {data.get('state','?')} | "
                  f"Temp: {data.get('temp_C','?')}C | "
                  f"RH: {data.get('RH_pct','?')}% | "
                  f"Ch1 diff/m3: {data.get('ch1_diff_m3','?')}")

        except KeyboardInterrupt:
            print("\nStopped by user")
            client.close()
            break
        except Exception as e:
            print(f"{datetime.now()} - Error: {e}")
            time.sleep(10)
            continue

        time.sleep(POLL_INTERVAL)

if __name__ == '__main__':
    main()
