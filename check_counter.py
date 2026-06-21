#!/usr/bin/env python3
"""Quick diagnostic script to check counter state"""

from pymodbus.client import ModbusTcpClient
import sys
import os

COUNTER_IP = '10.66.66.68'
COUNTER_PORT = 502

# Add decode functions
def decode_u32(registers):
    return (registers[1] << 16) | registers[0]

def connect():
    client = ModbusTcpClient(COUNTER_IP, port=COUNTER_PORT, timeout=5)
    if client.connect():
        return client
    return None

def main():
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║           COUNTER DIAGNOSTIC                              ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    print()

    client = connect()
    if not client:
        print("✗ Could not connect to counter")
        return 1

    print(f"✓ Connected to {COUNTER_IP}:{COUNTER_PORT}\n")

    try:
        # Get state
        r = client.read_holding_registers(address=5000, count=1)
        if not r.isError():
            state_map = {0:'Stopped', 1:'Delay', 2:'Counting', 3:'Hold'}
            state = state_map.get(r.registers[0], f'Unknown({r.registers[0]})')
            print(f"State: {state}")

        # Get record count
        r = client.read_holding_registers(address=8000, count=2)
        if not r.isError():
            count = decode_u32(r.registers)
            print(f"Records in counter: {count}")

        # Get sample params
        r = client.read_holding_registers(address=5003, count=2)
        if not r.isError():
            delay = decode_u32(r.registers)
            print(f"Delay time: {delay}s")

        r = client.read_holding_registers(address=5005, count=2)
        if not r.isError():
            sample = decode_u32(r.registers)
            print(f"Sample time: {sample}s")

        r = client.read_holding_registers(address=5007, count=2)
        if not r.isError():
            hold = decode_u32(r.registers)
            print(f"Hold time: {hold}s")

        r = client.read_holding_registers(address=5009, count=2)
        if not r.isError():
            cycles = decode_u32(r.registers)
            print(f"Cycles: {cycles}")

        # Check archive file
        print()
        archive_path = os.path.expanduser("~/particle_data/measurement_archive.csv")
        if os.path.exists(archive_path):
            with open(archive_path, 'r') as f:
                lines = f.readlines()
                if len(lines) > 1:
                    last_line = lines[-1]
                    parts = last_line.split(',')
                    if len(parts) > 2:
                        print(f"Archive last record: #{parts[0]} at {parts[1]} {parts[2]}")
                        print(f"Archive total records: {len(lines)-1}")
        else:
            print("Archive file not found at:", archive_path)

    finally:
        client.close()

    return 0

if __name__ == '__main__':
    sys.exit(main())
