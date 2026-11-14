# Capture the GATT traffic CortexService sends the EPOC-X during connect
# (the handshake that enables the properly-keyed encrypted EEG stream, which a
# bare BLE subscribe is missing).
#
# Breakpoints (all in libQt6Bluetooth, reached via these PLT stubs):
#   writeCharacteristic(this=rdi, const QLowEnergyCharacteristic&=rsi,
#       const QByteArray& value=rdx, WriteMode=ecx)        @ plt 0x1d3d40
#   writeDescriptor(this=rdi, const QLowEnergyDescriptor&=rsi,
#       const QByteArray& value=rdx)                       @ plt 0x1d08a0
#   discoverDetails(this=rdi, DiscoveryMode=esi)           @ plt 0x1d5ab0
# QByteArray: data ptr @ [obj+8], size @ [obj+0x10].
#
# USAGE -- ORDER MATTERS. The handshake fires once, at connect, so gdb must be
# attached BEFORE a fresh connect:
#   1. In EmotivPRO, STOP the session / DISCONNECT the headset (or just don't
#      let it connect yet). The headset must NOT already be streaming.
#   2. Attach the logger:
#        sudo gdb -q -batch -p "$(systemctl show -p MainPID --value cortex)" \
#          -x tools/dump_writes.gdb \
#          2>&1 | tee ~/eeg_writes.log
#   3. NOW trigger a fresh connect: reconnect in EmotivPRO (or power-cycle the
#      headset so it reconnects). The handshake writes fire -> #W / #D lines.
#   Self-halts after 120 events. If it hangs, Ctrl-C -- whatever logged is enough.

set pagination off
set height 0
set width 0

python
import gdb
base=None
for line in gdb.execute("info proc mappings", to_string=True).splitlines():
    p=line.split()
    if p and p[-1].endswith("/CortexService"): base=int(p[0],16); break
gdb.write("LOAD BASE = 0x%x\n"%base)
def rd(a,n):
    try: return gdb.selected_inferior().read_memory(a,n).tobytes()
    except Exception: return b""
def qba(obj):  # (data_ptr, size, value_bytes) of a QByteArray at address obj
    data=int.from_bytes(rd(obj+8,8),"little")
    size=int.from_bytes(rd(obj+0x10,8),"little")
    val=rd(data,size) if (data and 0<size<=4096) else b""
    return size,val
N=[0]; LIMIT=120

class WChar(gdb.Breakpoint):
    def __init__(self): super().__init__("*0x%x"%(base+0x1d3d40))
    def stop(self):
        f=gdb.selected_frame()
        rsi=int(f.read_register("rsi"))&(2**64-1)
        rdx=int(f.read_register("rdx"))&(2**64-1)
        ecx=int(f.read_register("ecx"))&0xffffffff
        size,val=qba(rdx)
        gdb.write("#W%03d writeCharacteristic mode=%d size=%d val=%s charobj=%s\n"
                  %(N[0],ecx,size,val.hex(),rd(rsi,24).hex()))
        N[0]+=1; return N[0]>=LIMIT

class WDesc(gdb.Breakpoint):
    def __init__(self): super().__init__("*0x%x"%(base+0x1d08a0))
    def stop(self):
        f=gdb.selected_frame()
        rsi=int(f.read_register("rsi"))&(2**64-1)
        rdx=int(f.read_register("rdx"))&(2**64-1)
        size,val=qba(rdx)
        gdb.write("#D%03d writeDescriptor size=%d val=%s descobj=%s\n"
                  %(N[0],size,val.hex(),rd(rsi,24).hex()))
        N[0]+=1; return N[0]>=LIMIT

class Disc(gdb.Breakpoint):
    def __init__(self): super().__init__("*0x%x"%(base+0x1d5ab0))
    def stop(self):
        gdb.write("#--- discoverDetails (fresh GATT discovery / connect marker)\n")
        return False

WChar(); WDesc(); Disc()
gdb.write("armed writeCharacteristic + writeDescriptor + discoverDetails loggers.\n")
gdb.write(">>> NOW trigger a FRESH connect (reconnect in EmotivPRO / power-cycle headset)\n")
end

continue
detach
quit
