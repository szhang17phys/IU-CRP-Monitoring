#!/usr/bin/env python3
"""
Force erase the particle counter memory.
Use this when you need a complete fresh start.

CAUTION: This will DELETE ALL DATA from the counter!
Make sure you've synced to archive first if you need the data.
"""

import sys
from pymodbus.client import ModbusTcpClient

# Counter connection details
COUNTER_IP = '10.66.66.68'
COUNTER_PORT = 502

def get_record_count(client):
    """Read how many records are in counter memory."""
    result = client.read_input_registers(address=8000, count=1)
    if hasattr(result, 'registers') and len(result.registers) > 0:
        return result.registers[0]
    elif hasattr(result, 'isError') and result.isError():
        raise Exception(f"Modbus error reading record count: {result}")
    return 0

def erase_counter(client):
    """Erase all data from counter memory."""
    print("Erasing counter memory...")
    client.write_registers(address=8004, values=[0x9559])
    import time
    time.sleep(3)
    remaining = get_record_count(client)
    print(f"Records remaining: {remaining}")
    return remaining == 0

def main():
    print("╔═══════════════════════════════════════════════════════════════╗")
    print("║        FORCE ERASE PARTICLE COUNTER                           ║")
    print("╚═══════════════════════════════════════════════════════════════╝")
    print()
    print(f"Counter: {COUNTER_IP}:{COUNTER_PORT}")
    print()

    # Connect
    client = ModbusTcpClient(COUNTER_IP, port=COUNTER_PORT, timeout=5)
    if not client.connect():
        print("✗ Failed to connect to counter!")
        return 1

    try:
        # Check current record count
        count = get_record_count(client)
        print(f"Current records in counter: {count}")
        print()

        if count == 0:
            print("✓ Counter is already empty!")
            return 0

        # Confirm
        print("⚠️  WARNING: This will DELETE ALL DATA from the counter!")
        print("    Make sure you've synced to archive first if you need the data.")
        print()
        response = input("Type 'ERASE' to confirm: ")

        if response != 'ERASE':
            print("Cancelled.")
            return 0

        # Erase
        print()
        success = erase_counter(client)

        if success:
            print("✓ Counter erased successfully!")
            print()
            print("Next steps:")
            print("  1. Start fresh monitoring: python3 particle_plus.py --all")
            print("  2. Counter will accumulate NEW data from this moment")
            return 0
        else:
            print("✗ Erase failed - counter still has records!")
            return 1

    finally:
        client.close()

if __name__ == '__main__':
    sys.exit(main())
