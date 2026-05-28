from pymodbus.client import ModbusTcpClient
import struct
import csv
import time
from datetime import datetime
import os

# ─── CONFIG ───────────────────────────────────────────────
COUNTER_IP       = '10.66.66.68'
PORT             = 502
OUTPUT_CSV       = '/home/rraut/particle_plus/particle_data_archive.csv'
ERASE_AFTER_SYNC = False   # set True only after confirming data looks correct
# ──────────────────────────────────────────────────────────


# ─── DATA TYPE DECODERS ───────────────────────────────────
# From register map:
#   U32/Float → Big-Endian WITHIN each register
#               Little-Endian ACROSS registers  (word swapped)
# This means registers[0] = LOW word, registers[1] = HIGH word

def decode_u32(registers):
    """Two 16-bit registers → unsigned 32-bit int (word swapped)"""
    low  = registers[0]
    high = registers[1]
    return (high << 16) | low

def decode_i32(registers):
    """Two 16-bit registers → signed 32-bit int (word swapped)"""
    low  = registers[0]
    high = registers[1]
    raw  = (high << 16) | low
    # interpret as signed
    if raw >= 0x80000000:
        raw -= 0x100000000
    return raw

def decode_float(registers):
    """Two 16-bit registers → IEEE 754 float (word swapped)"""
    low  = registers[0]
    high = registers[1]
    # pack high word first, then low word for correct float decode
    raw = struct.pack('>HH', high, low)
    return struct.unpack('>f', raw)[0]

def decode_string(registers):
    """Array of 16-bit registers → ASCII string
    Each register holds two chars: high byte first, then low byte
    Stop at null terminator"""
    result = ''
    for reg in registers:
        high = (reg >> 8) & 0xFF
        low  =  reg       & 0xFF
        if high == 0:
            break
        result += chr(high)
        if low == 0:
            break
        result += chr(low)
    return result.strip()
# ──────────────────────────────────────────────────────────


# ─── MODBUS HELPERS ───────────────────────────────────────

def get_record_count(client):
    """
    Register 8000: U32 (2 registers) = total number of stored records
    Returns integer count, or 0 on error
    """
    r = client.read_holding_registers(address=8000, count=2)
    if r.isError():
        print(f"  ERROR reading record count: {r}")
        return 0
    count = decode_u32(r.registers)
    return count


def latch_record(client, record_number):
    """
    Register 8002: I32 (2 registers, word swapped)
    Write record number to latch that record for reading.
    Special values:
        0  = current live data
        -1 = last saved record
        n  = specific record number
    After writing, all reads from 9000+ use that record's data
    until next latch.
    """
    if record_number >= 0:
        low  =  record_number        & 0xFFFF
        high = (record_number >> 16) & 0xFFFF
    else:
        # handle negative (e.g. -1 for last record)
        # convert to unsigned 32-bit two's complement
        unsigned = record_number & 0xFFFFFFFF
        low  =  unsigned        & 0xFFFF
        high = (unsigned >> 16) & 0xFFFF

    client.write_registers(address=8002, values=[low, high])
    time.sleep(0.3)   # give counter time to prepare the record


def read_latched_record(client):
    """
    Read all fields from whichever record is currently latched.
    Returns dict of all fields, or None if record is empty/invalid.
    """
    data = {}

    # ── Record number (I32, 2 registers) ──────────────────
    # Register 9000
    # -1 (0xFFFFFFFF) means no data available
    r = client.read_holding_registers(address=9000, count=2)
    if r.isError():
        return None
    rec_num = decode_i32(r.registers)
    if rec_num == -1:
        return None
    data['record_number'] = rec_num

    # ── Date string (11 registers) ────────────────────────
    # Register 9002, format YYYY-MM-DD
    r = client.read_holding_registers(address=9002, count=11)
    if not r.isError():
        data['date'] = decode_string(r.registers)
    else:
        data['date'] = ''

    # ── Time string (9 registers) ─────────────────────────
    # Register 9013, format hh:mm:ss
    r = client.read_holding_registers(address=9013, count=9)
    if not r.isError():
        data['time'] = decode_string(r.registers)
    else:
        data['time'] = ''

    # ── Location string (21 registers) ───────────────────
    # Register 9022
    r = client.read_holding_registers(address=9022, count=21)
    if not r.isError():
        data['location'] = decode_string(r.registers)
    else:
        data['location'] = ''

    # ── Sample duration (Float, 2 registers) ─────────────
    # Register 9074, units = seconds
    r = client.read_holding_registers(address=9074, count=2)
    if not r.isError():
        data['sample_duration_s'] = round(decode_float(r.registers), 2)
    else:
        data['sample_duration_s'] = None

    # ── Flow rate (Float, 2 registers) ───────────────────
    # Register 9076, units = CFM
    r = client.read_holding_registers(address=9076, count=2)
    if not r.isError():
        data['flow_CFM'] = round(decode_float(r.registers), 4)
    else:
        data['flow_CFM'] = None

    # ── Sample status bits (U16, 1 register) ─────────────
    # Register 9078
    # bit 0 = Laser OK
    # bit 1 = Flow OK
    # bit 2 = Temp OK
    # bit 3 = RH OK
    r = client.read_holding_registers(address=9078, count=1)
    if not r.isError():
        bits = r.registers[0]
        data['status_laser_ok'] = bool(bits & 0x0001)
        data['status_flow_ok']  = bool(bits & 0x0002)
        data['status_temp_ok']  = bool(bits & 0x0004)
        data['status_rh_ok']    = bool(bits & 0x0008)

    # ── Temperature (U16, 1 register) ────────────────────
    # Register 9079
    # LSB = 0.1 C
    # 999 = no sensor
    # 998 = sensor error
    r = client.read_holding_registers(address=9079, count=1)
    if not r.isError():
        raw = r.registers[0]
        if raw == 999:
            data['temp_C'] = None
        elif raw == 998:
            data['temp_C'] = None
        else:
            data['temp_C'] = round(raw * 0.1, 1)
    else:
        data['temp_C'] = None

    # ── Relative Humidity (U16, 1 register) ──────────────
    # Register 9080
    # LSB = 1%
    # 0 = no sensor
    # 1 = sensor error
    r = client.read_holding_registers(address=9080, count=1)
    if not r.isError():
        raw = r.registers[0]
        if raw <= 1:
            data['RH_pct'] = None
        else:
            data['RH_pct'] = raw
    else:
        data['RH_pct'] = None

    # ── 6 Particle Channels ───────────────────────────────
    # Each channel occupies 2 registers (Float = 32 bit)
    # So channel n offset from base = (n-1) * 2
    #
    # Base addresses:
    #   10100 = channel size (um)        Float
    #   10300 = differential counts      Float  (raw count in sample)
    #   10500 = differential counts/ft3  Float
    #   10700 = differential counts/m3   Float
    #   10900 = differential mass ug/m3  Float
    #   11100 = cumulative counts        Float
    #   11300 = cumulative counts/ft3    Float
    #   11500 = cumulative counts/m3     Float
    #   11700 = PM (sum mass ug/m3)      Float

    for i in range(6):
        offset = i * 2        # 2 registers per channel per data type
        ch     = f'ch{i+1}'

        # channel size in microns
        r = client.read_holding_registers(address=10100 + offset, count=2)
        if not r.isError():
            data[f'{ch}_size_um'] = round(decode_float(r.registers), 3)
        else:
            data[f'{ch}_size_um'] = None

        # differential raw counts (particles counted in this sample)
        r = client.read_holding_registers(address=10300 + offset, count=2)
        if not r.isError():
            data[f'{ch}_diff_counts'] = round(decode_float(r.registers), 1)
        else:
            data[f'{ch}_diff_counts'] = None

        # differential counts per cubic foot
        r = client.read_holding_registers(address=10500 + offset, count=2)
        if not r.isError():
            data[f'{ch}_diff_ft3'] = round(decode_float(r.registers), 2)
        else:
            data[f'{ch}_diff_ft3'] = None

        # differential counts per cubic meter
        r = client.read_holding_registers(address=10700 + offset, count=2)
        if not r.isError():
            data[f'{ch}_diff_m3'] = round(decode_float(r.registers), 2)
        else:
            data[f'{ch}_diff_m3'] = None

        # differential mass ug/m3
        r = client.read_holding_registers(address=10900 + offset, count=2)
        if not r.isError():
            data[f'{ch}_diff_mass_ugm3'] = round(decode_float(r.registers), 4)
        else:
            data[f'{ch}_diff_mass_ugm3'] = None

        # cumulative counts/m3
        r = client.read_holding_registers(address=11500 + offset, count=2)
        if not r.isError():
            data[f'{ch}_sum_m3'] = round(decode_float(r.registers), 2)
        else:
            data[f'{ch}_sum_m3'] = None

    return data
# ──────────────────────────────────────────────────────────


# ─── ERASE ────────────────────────────────────────────────

def erase_counter_memory(client):
    """
    Register 8004: write magic value 0x9559 to erase ALL records.
    This is irreversible. Only call after confirming CSV was saved.
    """
    print("  Erasing counter memory (writing 0x9559 to reg 8004)...")
    client.write_registers(address=8004, values=[0x9559])
    time.sleep(3)

    # verify by reading new count
    new_count = get_record_count(client)
    print(f"  Records remaining after erase: {new_count}")
    return new_count == 0
# ──────────────────────────────────────────────────────────


# ─── CSV ──────────────────────────────────────────────────

def save_to_csv(records, filepath):
    if not records:
        print("  No records to save")
        return False

    file_exists = os.path.exists(filepath)

    with open(filepath, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=records[0].keys())
        if not file_exists:
            writer.writeheader()
            print(f"  Created new CSV: {filepath}")
        writer.writerows(records)

    print(f"  Saved {len(records)} records → {filepath}")
    return True
# ──────────────────────────────────────────────────────────


# ─── MAIN SYNC LOGIC ──────────────────────────────────────

def sync(client):
    print(f"\n{'='*55}")
    print(f"Sync started: {datetime.now().isoformat()}")

    total = get_record_count(client)
    print(f"Records on counter: {total}")

    if total == 0:
        print("No records to sync — exiting")
        return True

    records = []
    failed  = []

    for i in range(1, total + 1):
        try:
            latch_record(client, i)
            data = read_latched_record(client)

            if data:
                records.append(data)
                print(f"  [{i:4d}/{total}] "
                      f"{data.get('date','?')} {data.get('time','?')} | "
                      f"temp={data.get('temp_C','?')}C  "
                      f"RH={data.get('RH_pct','?')}%  "
                      f"ch1_diff_m3={data.get('ch1_diff_m3','?')}")
            else:
                print(f"  [{i:4d}/{total}] Empty/invalid record, skipping")
                failed.append(i)

        except Exception as e:
            print(f"  [{i:4d}/{total}] Exception: {e}")
            failed.append(i)

    # ── save to CSV first, always ──────────────────────────
    saved_ok = save_to_csv(records, OUTPUT_CSV)

    # ── report any failures ────────────────────────────────
    if failed:
        print(f"\n  WARNING: {len(failed)} records failed: {failed}")
        print("  NOT erasing counter — fix failures first")
        return False

    # ── erase only if: all read OK + CSV saved + flag is True ──
    if ERASE_AFTER_SYNC and saved_ok and not failed:
        ok = erase_counter_memory(client)
        if ok:
            print("  Counter memory cleared successfully")
        else:
            print("  WARNING: Erase may be incomplete")
    elif not ERASE_AFTER_SYNC:
        print("  ERASE_AFTER_SYNC=False — counter memory kept intact")

    print(f"Sync complete: {len(records)} records saved")
    print(f"{'='*55}\n")
    return True


def main():
    print("Particle Counter Sync Tool")
    print(f"  Target : {COUNTER_IP}:{PORT}")
    print(f"  Output : {OUTPUT_CSV}")
    print(f"  Erase  : {ERASE_AFTER_SYNC}")
    print()

    client = ModbusTcpClient(COUNTER_IP, port=PORT, timeout=5)

    if not client.connect():
        print("ERROR: Could not connect to particle counter")
        return

    print("Connected successfully")

    try:
        sync(client)
    except KeyboardInterrupt:
        print("\nInterrupted — data NOT erased for safety")
    finally:
        client.close()
        print("Connection closed")


if __name__ == '__main__':
    main()
