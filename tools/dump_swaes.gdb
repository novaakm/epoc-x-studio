# Capture the EPOC-X AES-256 key from the SOFTWARE T-table AES the EEG path uses.
#   AES_decrypt(in=rdi,out=rsi,AES_KEY*=rdx) @ 0x2734820   (fires per block)
#   AES_set_encrypt_key(userKey=rdi,bits=rsi,AES_KEY*=rdx) @ 0x27336b0 (raw key!)
# AES_KEY: uint32 rd_key[60]; int rounds@0xf0.  rounds=14 => AES-256.
# We tally the decrypt schedule, grab matched (xor55-ciphertext -> plaintext)
# pairs via FinishBreakpoint, and dump the raw key directly if set_encrypt_key
# fires (toggle the EmotivPRO stream once to trigger it).
#
#   sudo gdb -q -batch -p "$(systemctl show -p MainPID --value cortex)" \
#     -x tools/dump_swaes.gdb \
#     2>&1 | tee ~/eeg_swaes.log

set pagination off
set height 0
set width 0

python
import gdb
from collections import Counter
base = None
for line in gdb.execute("info proc mappings", to_string=True).splitlines():
    p = line.split()
    if p and p[-1].endswith("/CortexService"):
        base = int(p[0], 16); break
gdb.write("LOAD BASE = 0x%x\n" % base)

def rd(a, k):
    try: return gdb.selected_inferior().read_memory(a, k).tobytes()
    except Exception: return b""

LIMIT = 30
n = [0]
sched = Counter()
scheds_full = []
pairs = []

class RetBP(gdb.FinishBreakpoint):
    def __init__(self, outptr, inblk):
        super().__init__(gdb.newest_frame(), internal=True)
        self.outptr = outptr; self.inblk = inblk; self.silent = True
    def stop(self):
        pairs.append((self.inblk, rd(self.outptr, 16).hex()))
        return False
    def out_of_scope(self):
        pass

class DecBP(gdb.Breakpoint):
    def __init__(self): super().__init__("*0x%x" % (base + 0x2734820))
    def stop(self):
        f = gdb.selected_frame()
        rdi = int(f.read_register("rdi")) & (2**64-1)
        rsi = int(f.read_register("rsi")) & (2**64-1)
        rdx = int(f.read_register("rdx")) & (2**64-1)
        rounds = int.from_bytes(rd(rdx+0xf0, 4) or b"\0"*4, "little")
        full = rd(rdx, 0xf0).hex()
        sched[(rounds, full)] += 1
        inblk = rd(rdi, 16).hex()
        if len(scheds_full) < 3:
            scheds_full.append((rounds, full, inblk))
        if len(pairs) < 8:
            try: RetBP(rsi, inblk)
            except Exception: pass
        n[0] += 1
        return n[0] >= LIMIT

class EncKeyBP(gdb.Breakpoint):
    def __init__(self): super().__init__("*0x%x" % (base + 0x27336b0))
    def stop(self):
        f = gdb.selected_frame()
        rdi = int(f.read_register("rdi")) & (2**64-1)
        rsi = int(f.read_register("rsi")) & (2**64-1)
        gdb.write("\n*** AES_set_encrypt_key: bits=%d RAWKEY=%s\n" % (rsi, rd(rdi, 32).hex()))
        return False

DecBP(); EncKeyBP()
gdb.write("armed; running (auto-stops at %d AES_decrypt hits)\n" % LIMIT)
end

continue

python
gdb.write("\n==== decrypt-schedule tally (hits : rounds) ====\n")
for (rounds, full), c in sched.most_common(6):
    gdb.write("  %4dx  rounds=%d  sched=%s\n" % (c, rounds, full))
gdb.write("\n-- sample (rounds, full-schedule, input-block) --\n")
for rounds, full, inb in scheds_full:
    gdb.write("  rounds=%d in=%s\n  sched=%s\n" % (rounds, inb, full))
gdb.write("\n-- matched (xor55'd ciphertext -> plaintext) pairs --\n")
for a, b in pairs:
    gdb.write("  in=%s out=%s\n" % (a, b))
gdb.write("total AES_decrypt hits: %d\n" % n[0])
end

detach
quit
