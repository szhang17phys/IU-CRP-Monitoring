from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient('10.66.114.55', port=502, timeout=5)
client.connect()

r = client.read_holding_registers(address=8000, count=2)
print('Raw registers:', r.registers)

# try both byte orders
count_normal  = (r.registers[0] << 16) | r.registers[1]
count_swapped = (r.registers[1] << 16) | r.registers[0]

print('Count normal:' , count_normal)
print('Count swapped:', count_swapped)

client.close()
